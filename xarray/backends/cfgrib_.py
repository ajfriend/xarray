import numpy as np

from .. import conventions
from ..core import indexing
from ..core.dataset import Dataset
from ..core.utils import Frozen, FrozenDict, close_on_error
from ..core.variable import Variable
from .common import AbstractDataStore, BackendArray
from .locks import SerializableLock, ensure_lock
from .plugins import BackendEntrypoint

# FIXME: Add a dedicated lock, even if ecCodes is supposed to be thread-safe
#   in most circumstances. See:
#       https://confluence.ecmwf.int/display/ECC/Frequently+Asked+Questions
ECCODES_LOCK = SerializableLock()


class CfGribArrayWrapper(BackendArray):
    def __init__(self, datastore, array):
        self.datastore = datastore
        self.shape = array.shape
        self.dtype = array.dtype
        self.array = array

    def __getitem__(self, key):
        return indexing.explicit_indexing_adapter(
            key, self.shape, indexing.IndexingSupport.OUTER, self._getitem
        )

    def _getitem(self, key):
        with self.datastore.lock:
            return self.array[key]


class CfGribDataStore(AbstractDataStore):
    """
    Implements the ``xr.AbstractDataStore`` read-only API for a GRIB file.
    """

    def __init__(self, filename, lock=None, **backend_kwargs):
        import cfgrib

        if lock is None:
            lock = ECCODES_LOCK
        self.lock = ensure_lock(lock)
        self.ds = cfgrib.open_file(filename, **backend_kwargs)

    def open_store_variable(self, name, var):
        if isinstance(var.data, np.ndarray):
            data = var.data
        else:
            wrapped_array = CfGribArrayWrapper(self, var.data)
            data = indexing.LazilyOuterIndexedArray(wrapped_array)

        encoding = self.ds.encoding.copy()
        encoding["original_shape"] = var.data.shape

        return Variable(var.dimensions, data, var.attributes, encoding)

    def get_variables(self):
        return FrozenDict(
            (k, self.open_store_variable(k, v)) for k, v in self.ds.variables.items()
        )

    def get_attrs(self):
        return Frozen(self.ds.attributes)

    def get_dimensions(self):
        return Frozen(self.ds.dimensions)

    def get_encoding(self):
        dims = self.get_dimensions()
        encoding = {"unlimited_dims": {k for k, v in dims.items() if v is None}}
        return encoding


def open_backend_dataset_cfgrib(
    filename_or_obj,
    *,
    mask_and_scale=True,
    decode_times=None,
    concat_characters=None,
    decode_coords=None,
    drop_variables=None,
    use_cftime=None,
    decode_timedelta=None,
    lock=None,
    indexpath="{path}.{short_hash}.idx",
    filter_by_keys={},
    read_keys=[],
    encode_cf=("parameter", "time", "geography", "vertical"),
    squeeze=True,
    time_dims=("time", "step"),
):

    store = CfGribDataStore(
        filename_or_obj,
        indexpath=indexpath,
        filter_by_keys=filter_by_keys,
        read_keys=read_keys,
        encode_cf=encode_cf,
        squeeze=squeeze,
        time_dims=time_dims,
        lock=lock,
    )

    with close_on_error(store):
        vars, attrs = store.load()
        file_obj = store
        encoding = store.get_encoding()

        vars, attrs, coord_names = conventions.decode_cf_variables(
            vars,
            attrs,
            mask_and_scale=mask_and_scale,
            decode_times=decode_times,
            concat_characters=concat_characters,
            decode_coords=decode_coords,
            drop_variables=drop_variables,
            use_cftime=use_cftime,
            decode_timedelta=decode_timedelta,
        )

        ds = Dataset(vars, attrs=attrs)
        ds = ds.set_coords(coord_names.intersection(vars))
        ds._file_obj = file_obj
        ds.encoding = encoding

    return ds


cfgrib_backend = BackendEntrypoint(open_dataset=open_backend_dataset_cfgrib)
