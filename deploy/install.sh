#!/usr/bin/env bash
# Install the benchmark service for the current user.
#
#   deploy/install.sh continuous|window [repository]
#
# The repository defaults to the checkout this script is run from, which is
# the one the service will then keep up to date.
#
# There is one unit, aihc-bench.service, whatever the mode: two runners on
# one machine measure each other's contention, which is how four published
# commits were spoiled once already.
set -euo pipefail

mode=${1:-}
case "$mode" in
  continuous | window) ;;
  *)
    echo "usage: $0 continuous|window [repository]" >&2
    exit 2
    ;;
esac

here=$(cd "$(dirname "$0")" && pwd)
repo=${2:-$(cd "$here/.." && pwd)}
units=$HOME/.config/systemd/user

install -d "$HOME/bin" "$units/aihc-bench.service.d"

# Rename into place rather than write over the file: bash reads a script as
# it runs it, and the service is usually mid-commit when this is installed.
# Overwriting the running file in place would have it resume at an offset
# into different text.
replace() {
  local source=$1 target=$2
  install -m 755 "$source" "$target.incoming"
  mv -f "$target.incoming" "$target"
}

replace "$here/aihc-bench-$mode.sh" "$HOME/bin/aihc-bench.sh"
replace "$here/aihc-bench-lib.sh" "$HOME/bin/aihc-bench-lib.sh"
install -m 644 "$here/aihc-bench.service" "$units/"
install -m 644 "$here/path.conf" "$units/aihc-bench.service.d/"
{
  echo "[Service]"
  echo "Environment=AIHC_BENCH_REPO=$repo"
} > "$units/aihc-bench.service.d/repository.conf"

systemctl --user daemon-reload
systemctl --user enable --now aihc-bench.service
systemctl --user --no-pager status aihc-bench.service | head -5
