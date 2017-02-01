from typing import Any, Optional, Dict, List

def rpc_run_fio(cfg: Dict[str, str]) -> Any: ...
def rpc_check_file_prefilled(path: str, used_size_mb: int) -> bool: ...
def rpc_prefill_test_files(files: Dict[str, int], force: bool = False, fio_path: str = 'fio') -> Optional[int]: ...


def load_fio_log_file(fname: str) -> List[float]: ...