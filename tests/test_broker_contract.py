from brokers.simulated import SimulatedBroker

from tests.contracts.broker_contract import assert_shared_broker_contract


async def test_simulated_broker_satisfies_shared_contract() -> None:
    await assert_shared_broker_contract(SimulatedBroker)
