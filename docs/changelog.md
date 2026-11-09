# Changelog

All notable changes are listed here, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Versioning on 0.x

- The public interface is every name in `even_simpler_salesforce.__all__` plus the documented
  submodules (`testing`, `observability`). Everything else is private, even if it has no
  leading underscore.
- While the version is 0.x, a **minor** release (0.1 → 0.2) may contain breaking changes. They
  are always listed under **Changed** or **Removed**, with migration notes.
- A public name is deprecated with `esf.EsfDeprecationWarning` for at least one minor release
  before it is removed.
- Each minor release pins a default Salesforce API version (68.0 for 0.1). Raising the minimum
  supported API version (59.0) is a minor release on 0.x.

## Unreleased

Changes that haven't been released yet are kept as
[towncrier](https://towncrier.readthedocs.io/) fragments in
[`changelog.d/`](https://github.com/Uttsavv/even-simpler-salesforce/tree/main/changelog.d), one
file per change. At release time they are compiled into the release notes below. To preview
them locally:

```sh
uvx towncrier build --draft --version Unreleased
```

## Released versions

Final releases are compiled from the fragments into
[`CHANGELOG.md`](https://github.com/Uttsavv/even-simpler-salesforce/blob/main/CHANGELOG.md),
which is included below as-is.

Pre-releases (`aN`, `bN`, `rcN`) keep their fragments in `changelog.d/`. Their notes are the
towncrier draft that the release workflow attaches to the
[GitHub Release](https://github.com/Uttsavv/even-simpler-salesforce/releases), and the same
entries appear here under the final version once it ships. The 0.1.0 release candidates
(starting with `v0.1.0rc1`, tagged on 2026-11-09) are in this state, so their changes are
still listed as fragments, and nothing appears below until 0.1.0 final.
`0.0.1` was a placeholder that reserved the PyPI name; it has no release notes.

--8<-- "CHANGELOG.md:18:"
