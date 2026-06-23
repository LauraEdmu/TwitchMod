from pathlib import Path
from compression import zstd

def get_logfiles(log_dir: Path = Path('.')) -> list[Path]:
    logfiles = [f for f in log_dir.glob('*.log') if f.is_file()]
    return logfiles

def compress_logs(
    logfiles: list[Path],
    level: int = 19,
    remove_raw: bool = True,
    noop: bool = False,
    ) -> int:
    if not logfiles:
        return 0

    for logfile in logfiles:
        target_file = logfile.with_suffix(logfile.suffix + ".zstd")

        if not noop:
            data = logfile.read_bytes()

            with zstd.open(str(target_file), "wb", level=level) as f:
                f.write(data)

            if remove_raw:
                logfile.unlink()
        else:
            print(f"Would write a compressed version of {logfile} to {target_file}")

            if remove_raw:
                print(f"Would unlink (remove) {logfile}")

    return len(logfiles)

def decompress_log(
    compressed_logfile: Path,
    remove_compressed: bool = False,
    noop: bool = False,
    ) -> bool:
    if not compressed_logfile.is_file():
        return False
    
    with zstd.open(compressed_logfile, 'rb') as f:
        data = f.read()

    target = compressed_logfile.with_suffix("")
    if not noop:
        target.write_bytes(data)
        
        if remove_compressed:
            compressed_logfile.unlink()
    else:
        print(f"Would write decompressed version of {compressed_logfile} to {target}")

        if remove_compressed:
            print(f"Would unlink (remove) {compressed_logfile}")

    return True

if __name__ == "__main__":
    mode = str(input("Do you want to:\n> [1] Compress all .log files\n> [2] Decompress a log file\n--> "))
    if mode.strip() not in ["1", "2"]:
        print("No valid mode selected")
        exit(1)

    if mode.strip() == "1":
        logfiles = get_logfiles()
        remove_raw_choice = str(input("Remove raw? (\"y/N\")"))
        remove_raw = True if remove_raw_choice.strip().lower() == "y" else False
        num = compress_logs(logfiles, remove_raw=remove_raw)
        print(f"Compressed {num} log files")
    elif mode.strip() == "2":
        compressed_logfile = Path(input("Enter the path to the compressed log file: "))
        remove_compressed_choice = str(input("Remove compressed? (\"y/N\")"))
        remove_compressed = True if remove_compressed_choice.strip().lower() == "y" else False
        success = decompress_log(compressed_logfile, remove_compressed=remove_compressed)
        if success:
            print(f"Decompressed {compressed_logfile}")
        else:
            print(f"Failed to decompress {compressed_logfile}")
