from argparse import Namespace
import copy
import itertools
import logging
from pathlib import Path
import pickle as pk
from typing import Optional, Set, Tuple, Union

import nltk.tokenize
import numpy as np
import sklearn.datasets
# noinspection PyProtectedMember
from sklearn.utils import Bunch

import torchtext
from torch.utils.data import Subset
from torchtext.data import Example, Field, Iterator, LabelField
import torchtext.datasets
from torchtext.data.dataset import Dataset
import torchtext.vocab

# Valid Choices - Any subset of: ('headers', 'footers', 'quotes')
from logger_utils import setup_logger
from pubn import BASE_DIR, NEG_LABEL, POS_LABEL, U_LABEL, TORCH_DEVICE

DATASET_REMOVE = ('headers', 'footers', 'quotes')
VALID_DATA_SUBSETS = ("train", "test", "all")

DATA_COL = "data"
LABEL_COL = "target"
LABEL_NAMES_COL = "target_names"

MAX_SEQ_LEN = 500


def _download_20newsgroups(subset: str, data_dir: Path, pos_cls: Set[int], neg_cls: Set[int]):
    r"""
    Gets the specified \p subset of the 20 Newsgroups dataset.  If necessary, the dataset is
    downloaded to directory \p data_dir.  It also tokenizes the import dataset

    :param data_dir: Path to the directory to sore
    :param subset: Valid choices, "train", "test", and "all"
    :return: Dataset
    """
    msg = f"Download {subset} 20 newsgroups dataset"
    logging.debug(f"Starting: {msg}")

    assert not data_dir.is_file(), "Must be a directory"
    assert subset in VALID_DATA_SUBSETS, "Invalid data subset"

    data_dir.mkdir(parents=True, exist_ok=True)
    dataset = sklearn.datasets.fetch_20newsgroups(data_home=data_dir, shuffle=False,
                                                  remove=DATASET_REMOVE, subset=subset)
    _filter_bunch_by_classes(dataset, cls_to_keep=pos_cls | neg_cls)

    logging.debug(f"COMPLETED: {msg}")
    return dataset


def _filter_bunch_by_classes(bunch: Bunch, cls_to_keep: Set[int]):
    r""" Removes any dataset items not in the specified class label lists """
    keep_idx = [val in cls_to_keep for val in bunch[LABEL_COL]]
    assert any(keep_idx), "No elements to keep list"

    def _filt_col(col_name: str):
        return list(itertools.compress(bunch[col_name], keep_idx))

    for key in bunch.keys():
        if isinstance(bunch[key], (list, np.ndarray)): continue
        bunch[key] = _filt_col(key)


def filter_dataset_by_cls(ds: Dataset, label_to_remove: Union[Set[int], int]) -> Subset:
    r""" Select a subset of the \p Dataset at the exclusion """
    if isinstance(label_to_remove, int): label_to_remove = {label_to_remove}

    indices = []
    for idx, x in enumerate(ds):
        if x.label in label_to_remove:
            indices.append(idx)

    # indices = torch.tensor(indices, dtype=torch.int64)
    return Subset(ds, indices)


def _filter_bunch_by_idx(bunch: Bunch, keep_idx):
    r"""
    Filters \p Bunch object and removes any unneeded elements

    :param bunch: Dataset \p Bunch object to filter
    :param keep_idx: List of Boolean values where the value is \p True if the element should be
                     kept.
    :return: Filtered \p Bunch object
    """
    bunch = copy.deepcopy(bunch)
    for key, val in bunch.items():
        if not isinstance(val, (list, np.ndarray)): continue
        if len(keep_idx) != len(val): continue

        bunch[key] = list(itertools.compress(val, keep_idx))
        if isinstance(val, np.ndarray): bunch[key] = np.asarray(bunch[key])
    return bunch


def _configure_binary_labels(bunch: Bunch, pos_cls: Set[int]):
    r""" Takes the 20 Newsgroup labels and binarizes them """
    def _is_pos(lbl: int) -> int:
        return POS_LABEL if lbl in pos_cls else NEG_LABEL

    bunch[LABEL_COL] = np.asarray(list(map(_is_pos, bunch[LABEL_COL])), dtype=np.int64)


def _select_positive_bunch(size_p: int, bunch: Bunch, pos_cls: Set[int],
                           remove_p_from_u: bool) -> Tuple[Bunch, Bunch]:
    r"""

    :param size_p: Number of (positive elements) to select from Bunch
    :param bunch: Training (unlabeled) \p Bunch
    :param pos_cls: List of positive classes in t
    :param remove_p_from_u: If \p True, elements in the positive (labeled) dataset are removed
                            from the unlabeled set.
    :return:
    """
    pos_idx = np.asarray([idx for idx, lbl in enumerate(bunch[LABEL_COL]) if lbl in pos_cls],
                         dtype=np.int32)

    assert len(pos_idx) >= size_p, "P set larger than the available data"
    np.random.shuffle(pos_idx)
    keep_idx = np.zeros_like(bunch[LABEL_COL], dtype=np.bool)
    for idx in pos_idx[:size_p]: keep_idx[idx] = True

    p_bunch = _filter_bunch_by_idx(bunch, keep_idx)
    p_bunch[LABEL_NAMES_COL] = [bunch[LABEL_NAMES_COL][idx] for idx in sorted(list(pos_cls))]
    if remove_p_from_u:
        bunch = _filter_bunch_by_idx(bunch, ~keep_idx)
    return p_bunch, bunch


def _bunch_to_ds(bunch: Bunch, text: Field, label: LabelField) -> Dataset:
    r""" Converts the \p bunch to a classification dataset """
    fields = [('text', text), ('label', label)]
    examples = [Example.fromlist(x, fields) for x in zip(bunch[DATA_COL], bunch[LABEL_COL])]
    return Dataset(examples, fields)


def _print_stats(text: Field, label: LabelField):
    r""" Log information about the dataset as a sanity check """
    logging.info(f"Length of Text Vocabulary: {str(len(text.vocab))}")
    logging.info(f"Vector size of Text Vocabulary: {text.vocab.vectors.shape[1]}")
    logging.info("Label Length: " + str(len(label.vocab)))


def _build_train_set(p_bunch: Bunch, u_bunch: Bunch, n_bunch: Optional[Bunch],
                     text: Field, label: LabelField) -> Dataset:
    r"""
    Convert the positive, negative, and unlabeled \p Bunch objects into a Dataset
    """
    data, labels, names = [], [], []
    for bunch, lbl in ((p_bunch, POS_LABEL), (u_bunch, U_LABEL), (n_bunch, NEG_LABEL)):
        if bunch is None: continue
        data.extend(bunch[DATA_COL])
        labels.append(np.full_like(bunch[LABEL_COL], lbl))

    t_bunch = copy.deepcopy(u_bunch)
    t_bunch[DATA_COL] = data
    t_bunch[LABEL_COL] = np.concatenate(labels, axis=0)
    return _bunch_to_ds(t_bunch, text, label)


def construct_iterator(ds: Dataset, bs: int, shuffle: bool = True) -> Iterator:
    r""" Construct \p Iterator which emulates a \p DataLoader """
    return Iterator(dataset=ds, batch_size=bs, shuffle=shuffle, device=TORCH_DEVICE)


def _pickle_filename(args: Namespace) -> Path:
    r""" File name for pickle file """
    serialize_dir = BASE_DIR / "tensors"
    serialize_dir.mkdir(parents=True, exist_ok=True)

    def _classes_to_str(cls_set: Set[int]) -> str:
        return ",".join([str(x) for x in sorted(cls_set)])

    fields = ["data", f"n-p={args.size_p}", f"n-n={args.size_n}",
              f"pos={_classes_to_str(args.pos)}", f"neg={_classes_to_str(args.neg)}"]

    fields[-1] += ".pk"
    return serialize_dir / "_".join(fields)


def _create_serialized_20newsgroups(serialize_path: Path, args):
    data_dir = BASE_DIR / ".data"
    cache_dir = data_dir / ".vector_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    complete_train = _download_20newsgroups("train", data_dir, args.pos, args.neg)

    tokenizer = nltk.tokenize.word_tokenize
    # noinspection PyPep8Naming
    TEXT = Field(sequential=True, tokenize=tokenizer, lower=True, include_lengths=True,
                 fix_length=MAX_SEQ_LEN)
    # noinspection PyPep8Naming
    LABEL = LabelField(sequential=False)
    complete_ds = _bunch_to_ds(complete_train, TEXT, LABEL)
    TEXT.build_vocab(complete_ds,
                     vectors=torchtext.vocab.GloVe(name="6B", dim=args.embed_dim, cache=cache_dir))

    p_bunch, u_bunch = _select_positive_bunch(args.size_p, complete_train, args.pos,
                                              remove_p_from_u=False)
    n_bunch = None  # ToDo Add code for getting the N bunch

    test_bunch = _download_20newsgroups("test", data_dir, args.pos, args.neg)

    # Binarize the labels
    for bunch in (p_bunch, u_bunch, test_bunch):
        _configure_binary_labels(bunch, pos_cls=args.pos)

    # Serialize 20 news groups
    msg = f"Writing serialized file {str(serialize_path)}"
    logging.debug(f"Starting: {msg}")
    with open(str(serialize_path), "wb+") as f_out:
        pk.dump((TEXT, LABEL, p_bunch, u_bunch, n_bunch, test_bunch), f_out)
    logging.debug(f"COMPLETED: {msg}")


def load_20newsgroups(args: Namespace):
    r"""
    Automatically downloads the 20 newsgroups dataset.
    :param args: Parsed command line arguments
    """
    assert args.pos and args.neg, "Class list empty"
    assert not (args.pos & args.neg), "Positive and negative classes not disjoint"

    # Load the serialized file if it exists
    serialize_path = _pickle_filename(args)
    if not serialize_path.exists():
        _create_serialized_20newsgroups(serialize_path, args)
    # _create_serialized_20newsgroups(serialize_path, args)

    with open(str(serialize_path), "rb") as f_in:
        msg = f"Loading serialized file: {str(serialize_path)}"
        logging.debug(f"Starting: {msg}")
        data = pk.load(f_in)
        # noinspection PyPep8Naming
        TEXT, LABEL, p_bunch, u_bunch, n_bunch, test_bunch = data
        logging.debug(f"COMPLETED: {msg}")

    train_ds = _build_train_set(p_bunch, u_bunch, n_bunch, TEXT, LABEL)
    # u_ds = _bunch_to_ds(u_bunch, TEXT, LABEL)
    # test_ds = _bunch_to_ds(test_bunch, TEXT, LABEL)
    u_ds = test_ds = None # ToDo Restore unlabel/test DS builder

    # LABEL.build_vocab(train_ds, test_ds)  # ToDo Restore LABEL Vocab builder
    LABEL.build_vocab(train_ds)
    _print_stats(TEXT, LABEL)
    return TEXT, LABEL, train_ds, test_ds, u_ds


def _main():
    args = Namespace()
    args.size_p = 1000
    args.size_n = 0
    args.embed_dim = 300
    args.pos, args.neg = set(range(0, 10)),  set(range(10, 20))

    # noinspection PyUnusedLocal
    newsgroups = load_20newsgroups(args)

    print("Completed")


if __name__ == "__main__":
    setup_logger()
    _main()
