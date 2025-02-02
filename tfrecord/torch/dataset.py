"""Load tfrecord files into torch datasets."""

import typing
import struct
import mmap
import os

import torch.utils.data

import numpy as np
from tfrecord import reader
from tfrecord import example_pb2
from tfrecord import iterator_utils


class TFRecordIO(object):
    def __init__(self,
                 data_path,
                 index_path,
                 description,
                 transform=None) -> None:
        super(TFRecordIO, self).__init__()

        fd = os.open(data_path, os.O_RDONLY)
        self.file = mmap.mmap(fd, 0, access=mmap.ACCESS_READ)

        if index_path is not None:
            self.indexs = np.loadtxt(index_path, dtype=np.int64, usecols=(0))
        else:
            self.indexs = None

        self.description = description

        self.typename_mapping = {
            "byte": "bytes_list",
            "float": "float_list",
            "int": "int64_list"
        }

        self.transform = transform or (lambda x: x)

    def set_indexs(self, index_path):
        self.indexs = np.loadtxt(index_path, dtype=np.int64, usecols=(0))

    def __getitem__(self, index):
        pos = self.indexs[index]
        data_bytes = self._extrate(pos)

        example = example_pb2.Example()
        example.ParseFromString(data_bytes)
        context = reader.extract_feature_dict(example.features,
                                              self.description,
                                              self.typename_mapping)

        if self.transform:
            context = self.transform(context)
        return context

    def _extrate(self, offset):
        self.file.seek(offset)
        byte_len_crc = self.file.read(12)
        length = struct.unpack('<Q', byte_len_crc[:8])[0]
        data_bytes = self.file.read(length)
        return data_bytes
    
    def __del__(self):
        if self.file:
            self.file.close()

    def __len__(self):
        return len(self.indexs)


class TFRecordDataset(torch.utils.data.IterableDataset):
    """Parse (generic) TFRecords dataset into `IterableDataset` object,
    which contain `np.ndarrays`s. By default (when `sequence_description`
    is None), it treats the TFRecords as containing `tf.Example`.
    Otherwise, it assumes it is a `tf.SequenceExample`.

    Params:
    -------
    data_path: str
        The path to the tfrecords file.

    index_path: str or None
        The path to the index file.

    description: list or dict of str, optional, default=None
        List of keys or dict of (key, value) pairs to extract from each
        record. The keys represent the name of the features and the
        values ("byte", "float", or "int") correspond to the data type.
        If dtypes are provided, then they are verified against the
        inferred type for compatibility purposes. If None (default),
        then all features contained in the file are extracted.

    shuffle_queue_size: int, optional, default=None
        Length of buffer. Determines how many records are queued to
        sample from.

    transform : a callable, default = None
        A function that takes in the input `features` i.e the dict
        provided in the description, transforms it and returns a
        desirable output.

    sequence_description: list or dict of str, optional, default=None
        Similar to `description`, but refers to the sequence features
        within a `SequenceExample`. When this field is `None`, then it
        is assumed that an `Example` is being read otherwise, a
        `SequenceExample` is read. If an empty list or dictionary is
        passed, then all features contained in the file are extracted.

    compression_type: str, optional, default=None
        The type of compression used for the tfrecord. Choose either
        'gzip' or None.

    """

    def __init__(self,
                 data_path: str,
                 index_path: typing.Union[str, None],
                 description: typing.Union[typing.List[str], typing.Dict[str, str], None] = None,
                 shuffle_queue_size: typing.Optional[int] = None,
                 transform: typing.Callable[[dict], typing.Any] = None,
                 sequence_description: typing.Union[typing.List[str], typing.Dict[str, str], None] = None,
                 compression_type: typing.Optional[str] = None,
                 ) -> None:
        super(TFRecordDataset, self).__init__()
        self.data_path = data_path
        self.index_path = index_path
        self.description = description
        self.sequence_description = sequence_description
        self.shuffle_queue_size = shuffle_queue_size
        self.transform = transform or (lambda x: x)
        self.compression_type = compression_type

    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is not None:
            shard = worker_info.id, worker_info.num_workers
            np.random.seed(worker_info.seed % np.iinfo(np.uint32).max)
        else:
            shard = None
        it = reader.tfrecord_loader(data_path=self.data_path,
                                    index_path=self.index_path,
                                    description=self.description,
                                    shard=shard,
                                    sequence_description=self.sequence_description,
                                    compression_type=self.compression_type)
        if self.shuffle_queue_size:
            it = iterator_utils.shuffle_iterator(it, self.shuffle_queue_size)
        if self.transform:
            it = map(self.transform, it)
        return it


class MultiTFRecordDataset(torch.utils.data.IterableDataset):
    """Parse multiple (generic) TFRecords datasets into an `IterableDataset`
    object, which contain `np.ndarrays`s.

    Params:
    -------
    data_pattern: str
        Input data path pattern.

    index_pattern: str or None
        Input index path pattern.

    splits: dict
        Dictionary of (key, value) pairs, where the key is used to
        construct the data and index path(s) and the value determines
        the contribution of each split to the batch.

    description: list or dict of str, optional, default=None
        List of keys or dict of (key, value) pairs to extract from each
        record. The keys represent the name of the features and the
        values ("byte", "float", or "int") correspond to the data type.
        If dtypes are provided, then they are verified against the
        inferred type for compatibility purposes. If None (default),
        then all features contained in the file are extracted.

    shuffle_queue_size: int, optional, default=None
        Length of buffer. Determines how many records are queued to
        sample from.

    transform : a callable, default = None
        A function that takes in the input `features` i.e the dict
        provided in the description, transforms it and returns a
        desirable output.

    sequence_description: list or dict of str, optional, default=None
        Similar to `description`, but refers to the sequence features
        within a `SequenceExample`. When this field is `None`, then it
        is assumed that an `Example` is being read otherwise, a
        `SequenceExample` is read. If an empty list or dictionary is
        passed, then all features contained in the file are extracted.

    compression_type: str, optional, default=None
        The type of compression used for the tfrecord. Choose either
        'gzip' or None.

    infinite: bool, optional, default=True
        Whether the Dataset should be infinite or not
    """

    def __init__(self,
                 data_pattern: str,
                 index_pattern: typing.Union[str, None],
                 splits: typing.Dict[str, float],
                 description: typing.Union[typing.List[str], typing.Dict[str, str], None] = None,
                 shuffle_queue_size: typing.Optional[int] = None,
                 transform: typing.Callable[[dict], typing.Any] = None,
                 sequence_description: typing.Union[typing.List[str], typing.Dict[str, str], None] = None,
                 compression_type: typing.Optional[str] = None,
                 infinite: bool = True
                 ) -> None:
        super(MultiTFRecordDataset, self).__init__()
        self.data_pattern = data_pattern
        self.index_pattern = index_pattern
        self.splits = splits
        self.description = description
        self.sequence_description = sequence_description
        self.shuffle_queue_size = shuffle_queue_size
        self.transform = transform
        self.compression_type = compression_type
        self.infinite = infinite

    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is not None:
            shard = worker_info.id, worker_info.num_workers
            np.random.seed(worker_info.seed % np.iinfo(np.uint32).max)
        else:
            shard = None
        it = reader.multi_tfrecord_loader(data_pattern=self.data_pattern,
                                          index_pattern=self.index_pattern,
                                          splits=self.splits,
                                          shard=shard,
                                          description=self.description,
                                          sequence_description=self.sequence_description,
                                          compression_type=self.compression_type,
                                          infinite=self.infinite,
                                          )
        if self.shuffle_queue_size:
            it = iterator_utils.shuffle_iterator(it, self.shuffle_queue_size)
        if self.transform:
            it = map(self.transform, it)
        return it
