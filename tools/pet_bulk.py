"""Run the full date-batched PET baseline with at most one bounded AI analysis."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from spl.pet_bulk import run_bulk


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--qc-only', action='store_true')
    args = parser.parse_args()
    directory, status = run_bulk(ROOT, args.profile, args.output, args.qc_only)
    print(json.dumps({'output': str(directory.relative_to(ROOT)), 'status': status}, sort_keys=True, ensure_ascii=False))


if __name__ == '__main__':
    main()
