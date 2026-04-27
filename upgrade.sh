#!/usr/bin/env bash
# Convenience alias for `./install.sh --upgrade`.
exec "$(dirname "${BASH_SOURCE[0]}")/install.sh" --upgrade "$@"
