import unittest

import ray
from ray.rllib.algorithms.ppo import PPOConfig


class LocalModeTest(unittest.TestCase):
    def setUp(self) -> None:
        ray.init(local_mode=True)

    def tearDown(self) -> None:
        ray.shutdown()

    def test_local(self):
        config = (
            PPOConfig()
            .environment("CartPole-v1")
            .env_runners(num_env_runners=2)
            .training(model={"fcnet_hiddens": [10]})
        )

        algo = config.build()
        print(algo.train())
        algo.stop()


if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main(["-v", __file__]))
