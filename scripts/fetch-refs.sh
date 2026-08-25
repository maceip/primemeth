#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
refs_dir="${repo_root}/refs"

mkdir -p "${refs_dir}"

clone_reference() {
	local name="$1"
	local url="$2"
	local expected_commit="$3"
	local destination="${refs_dir}/${name}"

	if [[ -e "${destination}" ]]; then
		if [[ ! -d "${destination}/.git" ]]; then
			printf 'refusing to replace non-git path: %s\n' "${destination}" >&2
			return 1
		fi

		local actual_origin
		if ! actual_origin="$(git -C "${destination}" remote get-url origin)"; then
			printf 'invalid reference repository: %s\n' "${destination}" >&2
			return 1
		fi
		if [[ "${actual_origin}" != "${url}" ]]; then
			printf 'unexpected origin for %s: %s\n' "${name}" "${actual_origin}" >&2
			return 1
		fi

		local actual_commit
		if ! actual_commit="$(git -C "${destination}" rev-parse --verify HEAD)"; then
			printf 'reference has no valid HEAD: %s\n' "${destination}" >&2
			return 1
		fi
		if [[ "${actual_commit}" != "${expected_commit}" ]]; then
			printf 'unexpected commit for %s: expected %s, found %s\n' "${name}" "${expected_commit}" "${actual_commit}" >&2
			return 1
		fi
		if [[ "$(git -C "${destination}" rev-parse --is-shallow-repository)" != "true" ]]; then
			printf 'reference is not shallow: %s\n' "${destination}" >&2
			return 1
		fi
		local worktree_status
		if ! worktree_status="$(git -C "${destination}" status --porcelain)"; then
			printf 'could not inspect reference worktree: %s\n' "${destination}" >&2
			return 1
		fi
		if [[ -n "${worktree_status}" ]]; then
			printf 'reference has local changes: %s\n' "${destination}" >&2
			return 1
		fi

		printf '%s %s\n' "${name}" "${actual_commit}"
		return
	fi

	local temporary_destination
	temporary_destination="$(mktemp -d "${refs_dir}/.${name}.XXXXXX")"
	if ! (
		set -e
		git -C "${temporary_destination}" init --quiet
		git -C "${temporary_destination}" remote add origin "${url}"
		git -C "${temporary_destination}" fetch --quiet --depth 1 --no-tags origin "${expected_commit}"
		git -C "${temporary_destination}" checkout --quiet --detach FETCH_HEAD
		[[ "$(git -C "${temporary_destination}" rev-parse --verify HEAD)" == "${expected_commit}" ]]
	); then
		rm -rf -- "${temporary_destination}"
		return 1
	fi
	mv "${temporary_destination}" "${destination}"
	printf '%s %s\n' "${name}" "${expected_commit}"
}

clone_reference \
	"reasoning-bank" \
	"https://github.com/google-research/reasoning-bank" \
	"ed80611788292ea739f1effd31f16c53823b8a0d"
clone_reference \
	"budget-aware-agent" \
	"https://github.com/google-research/budget-aware-agent" \
	"37d23f66c512734db476313cf436a506edbec9e0"
clone_reference \
	"AutoSaddler" \
	"https://github.com/microsoft/AutoSaddler" \
	"30e20ce004486c58e7ee97c66182a8d0d41ec90e"
