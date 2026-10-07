# Vendored bus card validator (test fixture only)

Byte copies of the fleet doctrine bus's R14 packet 2 validator, so
`tests/coordination/test_doctrine_outbox.py` can check a rendered card against the real format
with no network and no bus clone. The outbox itself never runs these copies: at drain time it
runs the `tools/validate-cards.mjs` carried by the fetched bus tip.

| File | Bus blob | Bus commit that landed it |
|---|---|---|
| `validate-cards.mjs` | `14c50e976cab9eb8faa9ca7e65a9f9d7bbee0fd3` | `0c78890` |
| `fleet-membership.mjs` | `e325e616d2b0ddcb16da42a7fe2d1d1c6a9b8cf0` | `179e527` |

`test_vendored_validator_is_byte_identical_to_the_bus_blobs` pins both blob ids. To refresh,
copy the files from the bus with `git cat-file blob <ref>:tools/<name>` and update the table
and the test together.
