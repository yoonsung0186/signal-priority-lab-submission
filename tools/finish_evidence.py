"""Create a new source-backed PET numerical evidence run; performs zero fits."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from spl.finish_evidence import run_evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pet-run', '--input-run', dest='pet_run', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--archive', action='append', help='Optional project-relative ZIP override, original input order and hashes retained')
    args = parser.parse_args()
    result = run_evidence(ROOT, args.pet_run, args.output, args.archive)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
