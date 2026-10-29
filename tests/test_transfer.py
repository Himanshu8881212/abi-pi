import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import transfer  # noqa: E402


def test_zip_round_trip_and_names_stay_in_their_folder():
    transfer.demo()
