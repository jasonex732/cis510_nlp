from argparse import Namespace
import logging
from functools import partial
from pathlib import Path
import time
from typing import Callable, Optional, Set, Union

import numpy as np

from fastai.basic_data import DataBunch
import torch
from torch import Tensor
import torch.nn as nn
# noinspection PyPep8Naming
import torch.nn.functional as F
import torch.optim as optim
from torchtext.data import Iterator, Dataset, LabelField, Example

from ._base_classifier import BaseClassifier, ClassifierConfig
from ._utils import BASE_DIR, IS_CUDA, NEG_LABEL, POS_LABEL, TORCH_DEVICE, U_LABEL, \
    construct_iterator, construct_filename
from .logger import TrainingLogger, create_stdout_handler
from .loss import LossType, PULoss


class NlpBiasedLearner(nn.Module):
    _log = None

    # def __init__(self, embedding: nn.Embedding, args: Namespace):
    def __init__(self, args: Namespace, embedding_weights: Tensor, prior: float,
                 rho: Optional[float]):
        super().__init__()
        self._setup_logger()

        self._log.debug(f"NLP Learner: Prior: {prior:.3f}")

        self._model = BaseClassifier(embedding_weights)

        self._args = args
        self.l_type = args.loss

        self.prior = prior
        self._rho = rho
        if self.l_type == LossType.PUBN:
            if self._rho is None: raise ValueError("rho required for PUbN loss")
            self._sigma = _SigmaLearner  # ToDo Fix Sigma learner constructor
        else:
            if self._rho is not None: raise ValueError("rho specified but PUbN loss not used")
            self._sigma = None

        self._prefix = self.l_type.name.lower()
        self._train_start = self._optim = self._best_loss = self._logger = None

        if IS_CUDA: self.cuda(TORCH_DEVICE)

    def fit(self, train: Dataset, valid: Dataset, labels: LabelField):
        r""" Fits the learner"""
        # Fields that apply regardless of loss method
        self._train_start = time.time()
        self._best_loss = np.inf

        tb_dir = BASE_DIR / "tb"
        TrainingLogger.create_tensorboard(tb_dir)
        # noinspection PyUnresolvedReferences
        self._logger = TrainingLogger(["Train Loss", "Valid Loss", "Best", "Time"],
                                      [20, 20, 7, 10],
                                      logger_name=self._model.Config.LOGGER_NAME,
                                      tb_grp_name=self.l_type.name)

        # noinspection PyUnresolvedReferences
        self._optim = optim.AdamW(self.parameters(), lr=self._model.Config.LEARNING_RATE,
                                  weight_decay=self._model.Config.WEIGHT_DECAY)

        # Filter the dataset based on the training configuration
        if self.l_type == LossType.NNPU:
            train = exclude_label_in_dataset(train, NEG_LABEL)
        elif self.l_type == LossType.PN:
            train = exclude_label_in_dataset(train, U_LABEL)
        # noinspection PyUnresolvedReferences
        train_itr = construct_iterator(train, bs=self._model.Config.BATCH_SIZE, shuffle=True)
        # noinspection PyUnresolvedReferences
        valid_itr = construct_iterator(valid, bs=self._model.Config.BATCH_SIZE, shuffle=True)

        if self.l_type == LossType.PUBN:
            self._fit_pubn(train_itr, valid_itr)
        elif self.l_type == LossType.NNPU or self.l_type == LossType.PN:

            self._fit_base(train_itr, valid_itr, labels, is_nnpu=self.l_type == LossType.NNPU)
        else:
            raise ValueError("Unknown loss type")

    def _fit_base(self, train: Iterator, valid: Iterator, labels: LabelField, is_nnpu: bool):
        r""" Shared functions for nnPU and supervised learning """
        map_pos = labels.vocab.stoi[POS_LABEL]

        def _supervised_loss(in_tensor: Tensor, target: Tensor) -> Tensor:
            y = torch.full(target.shape, -1)
            y[target == map_pos] = 1
            yx = in_tensor * y
            return -F.logsigmoid(yx).mean()

        if is_nnpu:
            pu_loss = PULoss(prior=self.prior, pos_label=map_pos)
            loss_func = partial(pu_loss.calc_loss)
            valid_loss = partial(pu_loss.zero_one_loss)
        else:
            loss_func = valid_loss = _supervised_loss

        # noinspection PyUnresolvedReferences
        for ep in range(1, self._model.Config.NUM_EPOCH + 1):
            train_loss, num_batch = torch.zeros(()), 0
            for batch in train:
                self._optim.zero_grad()
                # noinspection PyUnresolvedReferences
                dec_scores = self.forward(*batch.text)

                loss = loss_func(dec_scores, batch.label)
                if is_nnpu:
                    # noinspection PyUnresolvedReferences
                    loss.grad_var.backward()
                    # noinspection PyUnresolvedReferences
                    loss = loss.loss_var
                train_loss += loss.detach()
                num_batch += 1

                self._optim.step()
            train_loss /= num_batch
            self._log_epoch(ep, train_loss, valid, valid_loss)
        self._restore_best_model()

    def _fit_pubn(self, train: Iterator, valid_itr: Iterator):
        raise NotImplementedError("_fit_pubn not implemented")

    def _train_sigma(self, p_db: DataBunch, bn_db: DataBunch, u_db: DataBunch):
        raise NotImplementedError("sigma trainer not implemented")

    def _log_epoch(self, ep: int, train_loss: Tensor, valid_itr: Iterator, loss_func: Callable):
        r"""
        Log the results of the epoch

        :param ep: Epoch number
        :param train_loss: Training loss value
        :param valid_itr: Validation \p Iterator
        :param loss_func: Function used to calculate the loss
        """
        valid_loss = self._calc_valid_loss(valid_itr, loss_func)

        is_best = float(valid_loss.item()) < self._best_loss
        if is_best:
            self._best_loss = float(valid_loss.item())
            save_module(self, self._build_serialize_name(self._prefix))
        self._logger.log(ep, [train_loss, valid_loss, is_best, time.time() - self._train_start])

    def _calc_valid_loss(self, itr: Iterator, loss_func: Callable):
        r""" Calculate the validation loss for \p itr """
        self.eval()

        tot_loss, n_batch = torch.zeros(()), 0
        with torch.no_grad():
            for batch in itr:
                n_batch += 1
                tot_loss += loss_func(self.forward(*batch.text), batch.label).detach()

        return tot_loss / n_batch

    def forward(self, x: Tensor, x_len: Tensor) -> Tensor:
        # noinspection PyUnresolvedReferences
        return self._model.forward(x, x_len).squeeze()

    def _restore_best_model(self):
        r""" Restores the best trained model from disk """
        msg = f"Restoring {self.l_type.name} best trained model"
        self._log.debug(f"Starting: {msg}")
        load_module(self, self._build_serialize_name(self._prefix))
        self._log.debug(f"COMPLETED: {msg}")

    def _build_serialize_name(self, prefix: str) -> Path:
        r"""

        :param prefix: Prefix given to the name of the serialized file
        :return: \p Path to the serialized file
        """
        serialize_dir = BASE_DIR / "models"
        serialize_dir.mkdir(parents=True, exist_ok=True)
        return construct_filename(prefix, self._args, serialize_dir, "pth")

    @classmethod
    def _setup_logger(cls) -> None:
        r""" Creates a logger for just the ddPU class """
        if cls._log is not None: return
        cls._log = logging.getLogger(cls.Config.LOGGER_NAME)
        cls._log.propagate = False  # Do not propagate log messages to a parent logger
        create_stdout_handler(cls.Config.LOG_LEVEL, logger_name=cls.Config.LOGGER_NAME)


class _SigmaLearner(nn.Module):
    def __init__(self, args: Namespace, embedding_weights: Tensor, prior: float):
        self._model = BaseClassifier(embed=embedding_weights)

        self.prior, self.rho = prior, args.rho

        self._logger = None
        if IS_CUDA: self.cuda(TORCH_DEVICE)

    def fit(self, train: Iterator, valid: Iterator, labels: LabelField, is_nnpu: bool):
        r""" Shared functions for nnPU and supervised learning """
        map_pos, map_neg = labels.vocab.stoi[POS_LABEL], labels.vocab.stoi[NEG_LABEL]

        def _sigma_log_loss(in_tensor: Tensor, target: Tensor) -> Tensor:
            y = torch.full(target.shape, -1)
            for map_val in (map_pos, map_neg):
                y[target == map_val] = 1
            yx = in_tensor * y
            return -F.logsigmoid(yx).mean()

        pu_loss = PULoss(prior=self.prior, pos_label={map_pos, map_neg}, loss=_sigma_log_loss)
        loss_func = partial(pu_loss.calc_loss)
        valid_loss = partial(pu_loss.zero_one_loss)

        # noinspection PyUnresolvedReferences
        for ep in range(1, self._model.Config.NUM_EPOCH + 1):
            train_loss, num_batch = torch.zeros(()), 0
            for batch in train:
                self._optim.zero_grad()
                # noinspection PyUnresolvedReferences
                dec_scores = self._model.forward(*batch.text)

                loss = loss_func(dec_scores, batch.label)
                if is_nnpu:
                    # noinspection PyUnresolvedReferences
                    loss.grad_var.backward()
                    # noinspection PyUnresolvedReferences
                    loss = loss.loss_var
                train_loss += loss.detach()
                num_batch += 1

                self._optim.step()
            train_loss /= num_batch
            self._log_epoch(ep, train_loss, valid, valid_loss)
        self._restore_best_model()

    def forward(self, x: Tensor) -> Tensor:
        return F.sigmoid(self._net.forward(x).squeeze())


def save_module(module: nn.Module, filepath: Path) -> None:
    r""" Save the specified \p model to disk """
    filepath.parent.mkdir(parents=True, exist_ok=True)
    # noinspection PyUnresolvedReferences
    torch.save(module.state_dict(), str(filepath))


def load_module(module: nn.Module, filepath: Path):
    r"""
    Loads the specified model in file \p filepath into \p module and then returns \p module.

    :param module: \p Module where the module on disk will be loaded
    :param filepath: File where the \p Module is stored
    :return: Loaded model
    """
    # Map location allows for mapping model trained on any device to be loaded
    module.load_state_dict(torch.load(str(filepath), map_location=TORCH_DEVICE))
    # module.load_state_dict(torch.load(str(filepath)))

    module.eval()
    return module


def exclude_label_in_dataset(ds: Dataset, label_to_exclude: Union[int, Set[int]]) -> Dataset:
    r""" Creates a new \p Dataset that will exclude the label of \p label_to_exclude """
    def _filter_label(x: Example):
        # noinspection PyUnresolvedReferences
        return x.label not in label_to_exclude

    if isinstance(label_to_exclude, int): label_to_exclude = {label_to_exclude}
    return Dataset(examples=ds.examples, fields=ds.fields, filter_pred=_filter_label)
