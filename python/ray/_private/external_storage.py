import abc
import logging
import os
import random
import shutil
import time
import urllib
import uuid
from collections import namedtuple
from typing import IO, List, Optional, Tuple, Union

import ray
from ray._private.ray_constants import DEFAULT_OBJECT_PREFIX
from ray._raylet import ObjectRef

ParsedURL = namedtuple("ParsedURL", "base_url, offset, size")
logger = logging.getLogger(__name__)


def create_url_with_offset(*, url: str, offset: int, size: int) -> str:
    """Methods to create a URL with offset.

    When ray spills objects, it fuses multiple objects
    into one file to optimize the performance. That says, each object
    needs to keep tracking of its own special url to store metadata.

    This method creates an url_with_offset, which is used internally
    by Ray.

    Created url_with_offset can be passed to the self._get_base_url method
    to parse the filename used to store files.

    Example) file://path/to/file?offset=""&size=""

    Args:
        url: url to the object stored in the external storage.
        offset: Offset from the beginning of the file to
            the first bytes of this object.
        size: Size of the object that is stored in the url.
            It is used to calculate the last offset.

    Returns:
        url_with_offset stored internally to find
        objects from external storage.
    """
    return f"{url}?offset={offset}&size={size}"


def parse_url_with_offset(url_with_offset: str) -> Tuple[str, int, int]:
    """Parse url_with_offset to retrieve information.

    base_url is the url where the object ref
    is stored in the external storage.

    Args:
        url_with_offset: url created by create_url_with_offset.

    Returns:
        named tuple of base_url, offset, and size.
    """
    parsed_result = urllib.parse.urlparse(url_with_offset)
    query_dict = urllib.parse.parse_qs(parsed_result.query)
    # Split by ? to remove the query from the url.
    base_url = parsed_result.geturl().split("?")[0]
    if "offset" not in query_dict or "size" not in query_dict:
        raise ValueError(f"Failed to parse URL: {url_with_offset}")
    offset = int(query_dict["offset"][0])
    size = int(query_dict["size"][0])
    return ParsedURL(base_url=base_url, offset=offset, size=size)


class ExternalStorage(metaclass=abc.ABCMeta):
    """The base class for external storage.

    This class provides some useful functions for zero-copy object
    put/get from plasma store. Also it specifies the interface for
    object spilling.

    When inheriting this class, please make sure to implement validation
    logic inside __init__ method. When ray instance starts, it will
    instantiating external storage to validate the config.

    Raises:
        ValueError: when given configuration for
            the external storage is invalid.
    """

    HEADER_LENGTH = 24
    CORE_WORKER_INIT_GRACE_PERIOD_S = 1

    def __init__(self):
        # NOTE(edoakes): do not access this field directly. Use the `core_worker`
        # property instead to handle initialization race conditions.
        self._core_worker: Optional["ray._raylet.CoreWorker"] = None

    @property
    def core_worker(self) -> "ray._raylet.CoreWorker":
        """Get the core_worker initialized in this process.

        In rare cases, the core worker may not be fully initialized by the time an I/O
        worker begins to execute an operation because there is no explicit flag set to
        indicate that the Python layer is ready to execute tasks.
        """
        if self._core_worker is None:
            worker = ray._private.worker.global_worker
            start = time.time()
            while not worker.connected:
                time.sleep(0.001)
                if time.time() - start > self.CORE_WORKER_INIT_GRACE_PERIOD_S:
                    raise RuntimeError(
                        "CoreWorker didn't initialize within grace period of "
                        f"{self.CORE_WORKER_INIT_GRACE_PERIOD_S}s."
                    )

            self._core_worker = worker.core_worker

        return self._core_worker

    def _get_objects_from_store(self, object_refs):
        # Since the object should always exist in the plasma store before
        # spilling, it can directly get the object from the local plasma
        # store.
        # issue: https://github.com/ray-project/ray/pull/13831
        return self.core_worker.get_if_local(object_refs)

    def _put_object_to_store(
        self, metadata, data_size, file_like, object_ref, owner_address
    ):
        self.core_worker.put_file_like_object(
            metadata, data_size, file_like, object_ref, owner_address
        )

    def _write_multiple_objects(
        self, f: IO, object_refs: List[ObjectRef], owner_addresses: List[str], url: str
    ) -> List[str]:
        """Fuse all given objects into a given file handle.

        Args:
            f: File handle to fusion all given object refs.
            object_refs: Object references to fusion to a single file.
            owner_addresses: Owner addresses for the provided objects.
            url: url where the object ref is stored
                in the external storage.

        Return:
            List of urls_with_offset of fused objects.
            The order of returned keys are equivalent to the one
            with given object_refs.
        """
        keys = []
        offset = 0
        ray_object_pairs = self._get_objects_from_store(object_refs)
        for ref, (buf, metadata, _), owner_address in zip(
            object_refs, ray_object_pairs, owner_addresses
        ):
            address_len = len(owner_address)
            metadata_len = len(metadata)
            if buf is None and len(metadata) == 0:
                error = f"Object {ref.hex()} does not exist."
                raise ValueError(error)
            buf_len = 0 if buf is None else len(buf)
            payload = (
                address_len.to_bytes(8, byteorder="little")
                + metadata_len.to_bytes(8, byteorder="little")
                + buf_len.to_bytes(8, byteorder="little")
                + owner_address
                + metadata
                + (memoryview(buf) if buf_len else b"")
            )
            # 24 bytes to store owner address, metadata, and buffer lengths.
            payload_len = len(payload)
            assert (
                self.HEADER_LENGTH + address_len + metadata_len + buf_len == payload_len
            )
            written_bytes = f.write(payload)
            assert written_bytes == payload_len
            url_with_offset = create_url_with_offset(
                url=url, offset=offset, size=written_bytes
            )
            keys.append(url_with_offset.encode())
            offset += written_bytes
        # Necessary because pyarrow.io.NativeFile does not flush() on close().
        f.flush()
        return keys

    def _size_check(self, address_len, metadata_len, buffer_len, obtained_data_size):
        """Check whether or not the obtained_data_size is as expected.

        Args:
            address_len: Length of the address.
            metadata_len: Actual metadata length of the object.
            buffer_len: Actual buffer length of the object.
            obtained_data_size: Data size specified in the url_with_offset.

        Raises:
            ValueError: If obtained_data_size is different from
                address_len + metadata_len + buffer_len + 24 (first 8 bytes to store length).
        """
        data_size_in_bytes = (
            address_len + metadata_len + buffer_len + self.HEADER_LENGTH
        )
        if data_size_in_bytes != obtained_data_size:
            raise ValueError(
                f"Obtained data has a size of {data_size_in_bytes}, "
                "although it is supposed to have the "
                f"size of {obtained_data_size}."
            )

    @abc.abstractmethod
    def spill_objects(self, object_refs, owner_addresses) -> List[str]:
        """Spill objects to the external storage. Objects are specified
        by their object refs.

        Args:
            object_refs: The list of the refs of the objects to be spilled.
            owner_addresses: Owner addresses for the provided objects.
        Returns:
            A list of internal URLs with object offset.
        """

    @abc.abstractmethod
    def restore_spilled_objects(
        self, object_refs: List[ObjectRef], url_with_offset_list: List[str]
    ) -> int:
        """Restore objects from the external storage.

        Args:
            object_refs: List of object IDs (note that it is not ref).
            url_with_offset_list: List of url_with_offset.

        Returns:
            The total number of bytes restored.
        """

    @abc.abstractmethod
    def delete_spilled_objects(self, urls: List[str]):
        """Delete objects that are spilled to the external storage.

        Args:
            urls: URLs that store spilled object files.

        NOTE: This function should not fail if some of the urls
        do not exist.
        """

    @abc.abstractmethod
    def destroy_external_storage(self):
        """Destroy external storage when a head node is down.

        NOTE: This is currently working when the cluster is
        started by ray.init
        """


class NullStorage(ExternalStorage):
    """The class that represents an uninitialized external storage."""

    def spill_objects(self, object_refs, owner_addresses) -> List[str]:
        raise NotImplementedError("External storage is not initialized")

    def restore_spilled_objects(self, object_refs, url_with_offset_list):
        raise NotImplementedError("External storage is not initialized")

    def delete_spilled_objects(self, urls: List[str]):
        raise NotImplementedError("External storage is not initialized")

    def destroy_external_storage(self):
        raise NotImplementedError("External storage is not initialized")


class FileSystemStorage(ExternalStorage):
    """The class for filesystem-like external storage.

    Raises:
        ValueError: Raises directory path to
            spill objects doesn't exist.
    """

    def __init__(
        self,
        node_id: str,
        directory_path: Union[str, List[str]],
        buffer_size: Optional[int] = None,
    ):
        super().__init__()

        # -- A list of directory paths to spill objects --
        self._directory_paths = []
        # -- Current directory to spill objects --
        self._current_directory_index = 0
        # -- File buffer size to spill objects --
        self._buffer_size = -1

        # Validation.
        assert (
            directory_path is not None
        ), "directory_path should be provided to use object spilling."
        if isinstance(directory_path, str):
            directory_path = [directory_path]
        assert isinstance(
            directory_path, list
        ), "Directory_path must be either a single string or a list of strings"
        if buffer_size is not None:
            assert isinstance(buffer_size, int), "buffer_size must be an integer."
            self._buffer_size = buffer_size

        # Create directories.
        for path in directory_path:
            full_dir_path = os.path.join(path, f"{DEFAULT_OBJECT_PREFIX}_{node_id}")
            os.makedirs(full_dir_path, exist_ok=True)
            if not os.path.exists(full_dir_path):
                raise ValueError(
                    "The given directory path to store objects, "
                    f"{full_dir_path}, could not be created."
                )
            self._directory_paths.append(full_dir_path)
        assert len(self._directory_paths) == len(directory_path)
        # Choose the current directory.
        # It chooses a random index to maximize multiple directories that are
        # mounted at different point.
        self._current_directory_index = random.randrange(0, len(self._directory_paths))

    def spill_objects(self, object_refs, owner_addresses) -> List[str]:
        if len(object_refs) == 0:
            return []
        # Choose the current directory path by round robin order.
        self._current_directory_index = (self._current_directory_index + 1) % len(
            self._directory_paths
        )
        directory_path = self._directory_paths[self._current_directory_index]

        filename = _get_unique_spill_filename(object_refs)
        url = f"{os.path.join(directory_path, filename)}"
        with open(url, "wb", buffering=self._buffer_size) as f:
            return self._write_multiple_objects(f, object_refs, owner_addresses, url)

    def restore_spilled_objects(
        self, object_refs: List[ObjectRef], url_with_offset_list: List[str]
    ):
        total = 0
        for i in range(len(object_refs)):
            object_ref = object_refs[i]
            url_with_offset = url_with_offset_list[i].decode()
            # Retrieve the information needed.
            parsed_result = parse_url_with_offset(url_with_offset)
            base_url = parsed_result.base_url
            offset = parsed_result.offset
            # Read a part of the file and recover the object.
            with open(base_url, "rb") as f:
                f.seek(offset)
                address_len = int.from_bytes(f.read(8), byteorder="little")
                metadata_len = int.from_bytes(f.read(8), byteorder="little")
                buf_len = int.from_bytes(f.read(8), byteorder="little")
                self._size_check(address_len, metadata_len, buf_len, parsed_result.size)
                total += buf_len
                owner_address = f.read(address_len)
                metadata = f.read(metadata_len)
                # read remaining data to our buffer
                self._put_object_to_store(
                    metadata, buf_len, f, object_ref, owner_address
                )
        return total

    def delete_spilled_objects(self, urls: List[str]):
        for url in urls:
            path = parse_url_with_offset(url.decode()).base_url
            try:
                os.remove(path)
            except FileNotFoundError:
                # Occurs when the urls are retried during worker crash/failure.
                pass

    def destroy_external_storage(self):
        for directory_path in self._directory_paths:
            self._destroy_external_storage(directory_path)

    def _destroy_external_storage(self, directory_path):
        # There's a race condition where IO workers are still
        # deleting each objects while we try deleting the
        # whole directory. So we should keep trying it until
        # The directory is actually deleted.
        while os.path.isdir(directory_path):
            try:
                shutil.rmtree(directory_path)
            except (FileNotFoundError):
                # If exception occurs when other IO workers are
                # deleting the file at the same time.
                pass
            except Exception:
                logger.exception(
                    "Error cleaning up spill files. "
                    "You might still have remaining spilled "
                    "objects inside `ray_spilled_objects` directory."
                )
                break


class ExternalStorageSmartOpenImpl(ExternalStorage):
    """The external storage class implemented by smart_open.
    (https://github.com/RaRe-Technologies/smart_open)

    Smart open supports multiple backend with the same APIs.

    To use this implementation, you should pre-create the given uri.
    For example, if your uri is a local file path, you should pre-create
    the directory.

    Args:
        uri: Storage URI used for smart open.
        prefix: Prefix of objects that are stored.
        override_transport_params: Overriding the default value of
            transport_params for smart-open library.

    Raises:
        ModuleNotFoundError: If it fails to setup.
            For example, if smart open library
            is not downloaded, this will fail.
    """

    def __init__(
        self,
        node_id: str,
        uri: str or list,
        override_transport_params: dict = None,
        buffer_size=1024 * 1024,  # For remote spilling, at least 1MB is recommended.
    ):
        super().__init__()

        try:
            from smart_open import open  # noqa
        except ModuleNotFoundError as e:
            raise ModuleNotFoundError(
                "Smart open is chosen to be a object spilling "
                "external storage, but smart_open and boto3 "
                f"is not downloaded. Original error: {e}"
            )

        # Validation
        assert uri is not None, "uri should be provided to use object spilling."
        if isinstance(uri, str):
            uri = [uri]
        assert isinstance(uri, list), "uri must be a single string or list of strings."
        assert isinstance(buffer_size, int), "buffer_size must be an integer."

        uri_is_s3 = [u.startswith("s3://") for u in uri]
        self.is_for_s3 = all(uri_is_s3)
        if not self.is_for_s3:
            assert not any(uri_is_s3), "all uri's must be s3 or none can be s3."
            self._uris = uri
        else:
            self._uris = [u.strip("/") for u in uri]
        assert len(self._uris) == len(uri)

        self._current_uri_index = random.randrange(0, len(self._uris))
        self.prefix = f"{DEFAULT_OBJECT_PREFIX}_{node_id}"
        self.override_transport_params = override_transport_params or {}

        if self.is_for_s3:
            import boto3  # noqa

            # Setup boto3. It is essential because if we don't create boto
            # session, smart_open will create a new session for every
            # open call.
            self.s3 = boto3.resource(service_name="s3")

            # smart_open always seek to 0 if we don't set this argument.
            # This will lead us to call a Object.get when it is not necessary,
            # so defer seek and call seek before reading objects instead.
            self.transport_params = {
                "defer_seek": True,
                "resource": self.s3,
                "buffer_size": buffer_size,
            }
        else:
            self.transport_params = {}

        self.transport_params.update(self.override_transport_params)

    def spill_objects(self, object_refs, owner_addresses) -> List[str]:
        if len(object_refs) == 0:
            return []
        from smart_open import open

        # Choose the current uri by round robin order.
        self._current_uri_index = (self._current_uri_index + 1) % len(self._uris)
        uri = self._uris[self._current_uri_index]

        key = f"{self.prefix}-{_get_unique_spill_filename(object_refs)}"
        url = f"{uri}/{key}"

        with open(
            url,
            mode="wb",
            transport_params=self.transport_params,
        ) as file_like:
            return self._write_multiple_objects(
                file_like, object_refs, owner_addresses, url
            )

    def restore_spilled_objects(
        self, object_refs: List[ObjectRef], url_with_offset_list: List[str]
    ):
        from smart_open import open

        total = 0
        for i in range(len(object_refs)):
            object_ref = object_refs[i]
            url_with_offset = url_with_offset_list[i].decode()

            # Retrieve the information needed.
            parsed_result = parse_url_with_offset(url_with_offset)
            base_url = parsed_result.base_url
            offset = parsed_result.offset

            with open(base_url, "rb", transport_params=self.transport_params) as f:
                # smart open seek reads the file from offset-end_of_the_file
                # when the seek is called.
                f.seek(offset)
                address_len = int.from_bytes(f.read(8), byteorder="little")
                metadata_len = int.from_bytes(f.read(8), byteorder="little")
                buf_len = int.from_bytes(f.read(8), byteorder="little")
                self._size_check(address_len, metadata_len, buf_len, parsed_result.size)
                owner_address = f.read(address_len)
                total += buf_len
                metadata = f.read(metadata_len)
                # read remaining data to our buffer
                self._put_object_to_store(
                    metadata, buf_len, f, object_ref, owner_address
                )
        return total

    def delete_spilled_objects(self, urls: List[str]):
        pass

    def destroy_external_storage(self):
        pass


_external_storage = NullStorage()


class UnstableFileStorage(FileSystemStorage):
    """This class is for testing with writing failure."""

    def __init__(self, node_id: str, **kwargs):
        super().__init__(node_id, **kwargs)
        self._failure_rate = 0.1
        self._partial_failure_ratio = 0.2

    def spill_objects(self, object_refs, owner_addresses) -> List[str]:
        r = random.random() < self._failure_rate
        failed = r < self._failure_rate
        partial_failed = r < self._partial_failure_ratio
        if failed:
            raise IOError("Spilling object failed intentionally for testing.")
        elif partial_failed:
            i = random.choice(range(len(object_refs)))
            return super().spill_objects(object_refs[:i], owner_addresses)
        else:
            return super().spill_objects(object_refs, owner_addresses)


class SlowFileStorage(FileSystemStorage):
    """This class is for testing slow object spilling."""

    def __init__(self, node_id: str, **kwargs):
        super().__init__(node_id, **kwargs)
        self._min_delay = 1
        self._max_delay = 2

    def spill_objects(self, object_refs, owner_addresses) -> List[str]:
        delay = random.random() * (self._max_delay - self._min_delay) + self._min_delay
        time.sleep(delay)
        return super().spill_objects(object_refs, owner_addresses)


def setup_external_storage(config, node_id, session_name):
    """Setup the external storage according to the config."""
    assert node_id is not None, "node_id should be provided."
    global _external_storage
    if config:
        storage_type = config["type"]
        if storage_type == "filesystem":
            _external_storage = FileSystemStorage(node_id, **config["params"])
        elif storage_type == "smart_open":
            _external_storage = ExternalStorageSmartOpenImpl(
                node_id, **config["params"]
            )
        elif storage_type == "mock_distributed_fs":
            # This storage is used to unit test distributed external storages.
            # TODO(sang): Delete it after introducing the mock S3 test.
            _external_storage = FileSystemStorage(node_id, **config["params"])
        elif storage_type == "unstable_fs":
            # This storage is used to unit test unstable file system for fault
            # tolerance.
            _external_storage = UnstableFileStorage(node_id, **config["params"])
        elif storage_type == "slow_fs":
            # This storage is used to unit test slow filesystems.
            _external_storage = SlowFileStorage(node_id, **config["params"])
        else:
            raise ValueError(f"Unknown external storage type: {storage_type}")
    else:
        _external_storage = NullStorage()
    return _external_storage


def reset_external_storage():
    global _external_storage
    _external_storage = NullStorage()


def spill_objects(object_refs, owner_addresses):
    """Spill objects to the external storage. Objects are specified
    by their object refs.

    Args:
        object_refs: The list of the refs of the objects to be spilled.
        owner_addresses: The owner addresses of the provided object refs.
    Returns:
        A list of keys corresponding to the input object refs.
    """
    return _external_storage.spill_objects(object_refs, owner_addresses)


def restore_spilled_objects(
    object_refs: List[ObjectRef], url_with_offset_list: List[str]
):
    """Restore objects from the external storage.

    Args:
        object_refs: List of object IDs (note that it is not ref).
        url_with_offset_list: List of url_with_offset.
    """
    return _external_storage.restore_spilled_objects(object_refs, url_with_offset_list)


def delete_spilled_objects(urls: List[str]):
    """Delete objects that are spilled to the external storage.

    Args:
        urls: URLs that store spilled object files.
    """
    _external_storage.delete_spilled_objects(urls)


def _get_unique_spill_filename(object_refs: List[ObjectRef]):
    """Generate a unqiue spill file name.

    Args:
        object_refs: objects to be spilled in this file.
    """
    return f"{uuid.uuid4().hex}-multi-{len(object_refs)}"
