# Rulings (OFFLINE TEST FIXTURE - not doctrine)

This tree stands in for the fleet doctrine bus tip in hermetic tests only
(tools/coordination/conftest.py points MLV_DOCTRINE_FIXTURE_ROOT here). It carries
no authority. Production composition fetches the real bus and refuses when it cannot.

- Pull is the only direction.
- Doctrine repo is the bus.
