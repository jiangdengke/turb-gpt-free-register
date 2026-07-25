import json
import unittest
from unittest.mock import Mock, patch

from core.codex_agent import (
    AGENT_HARNESS_ID,
    AGENT_VERSION,
    RUNNING_LOCATION,
    AgentRegistryNotEnabledError,
    register_agent,
    register_task,
)


class CodexAgentProtocolTests(unittest.TestCase):
    @patch("core.codex_agent._agent_post")
    def test_register_agent_uses_current_payload(self, agent_post):
        response = Mock(status_code=201, text="")
        response.json.return_value = {"agent_runtime_id": "agent-runtime-1"}
        agent_post.return_value = response

        runtime_id = register_agent("access-token", "ssh-ed25519 public-key")

        self.assertEqual(runtime_id, "agent-runtime-1")
        self.assertEqual(
            agent_post.call_args.kwargs["payload"],
            {
                "abom": {
                    "agent_version": AGENT_VERSION,
                    "agent_harness_id": AGENT_HARNESS_ID,
                    "running_location": RUNNING_LOCATION,
                },
                "agent_public_key": "ssh-ed25519 public-key",
                "capabilities": ["responsesapi"],
                "ttl": None,
            },
        )
        self.assertEqual(agent_post.call_count, 1)

    @patch("core.codex_agent._agent_post")
    def test_register_agent_classifies_disabled_registry(self, agent_post):
        body = {
            "error": {
                "message": "Agent registry is not enabled.",
                "code": "agent_registry_not_enabled",
            }
        }
        response = Mock(status_code=403, text=json.dumps(body))
        response.json.return_value = body
        agent_post.return_value = response

        with self.assertRaisesRegex(AgentRegistryNotEnabledError, "未开放 Agent Registry"):
            register_agent("access-token", "ssh-ed25519 public-key")

    @patch("core.codex_agent._agent_post")
    @patch("core.codex_agent.load_pem_private_key")
    def test_register_task_accepts_current_task_id_field(self, load_key, agent_post):
        private_key = Mock()
        private_key.sign.return_value = b"signed"
        load_key.return_value = private_key
        response = Mock(status_code=200, text="")
        response.json.return_value = {"task_id": "task-1"}
        agent_post.return_value = response

        task_id = register_task(
            "access-token",
            "agent-runtime-1",
            "MC4CAQAwBQYDK2VwBCIEIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        )

        self.assertEqual(task_id, "task-1")


if __name__ == "__main__":
    unittest.main()
