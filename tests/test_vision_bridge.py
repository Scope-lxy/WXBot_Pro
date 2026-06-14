import unittest
from types import SimpleNamespace

from core.vision_bridge import VisionBridge


class OpenAILikeAPI:
    DS_NOW_MOD = "vision-model"


class DusLikeAPI:
    DS_NOW_MOD = "vision-model"


class VisionBridgeSignatureTests(unittest.TestCase):
    def test_recognition_signature_unwraps_counting_proxy(self):
        openai_proxy = SimpleNamespace(_api=OpenAILikeAPI())
        dus_proxy = SimpleNamespace(_api=DusLikeAPI())

        self.assertEqual(
            VisionBridge._recognition_signature(openai_proxy),
            "OpenAILikeAPI:vision-model",
        )
        self.assertEqual(
            VisionBridge._recognition_signature(dus_proxy),
            "DusLikeAPI:vision-model",
        )


if __name__ == "__main__":
    unittest.main()
