"""仅在远程应用已交付 assembly 夹具修正；写入前核验完整目标摘要。"""

from hashlib import sha256
from pathlib import Path

path = Path("tests/test_strategy_assembly.py")
original = path.read_bytes()
updated = original.replace(
    b'metadata={"strategy_id": order.strategy_id,',
    b'metadata={**order.metadata, "strategy_id": order.strategy_id,',
)
marker = b"        self.assertEqual(client.report_errors, ())\n"
if b"self.assertEqual(strategy.fills_received, 1)" not in updated:
    assertions = (
        b"        self.assertEqual(strategy.position(strategy.config.target_key), 1)\n"
        b"        self.assertEqual(strategy.last_fill_position, 1)\n"
        b"        self.assertEqual(strategy.fills_received, 1)\n"
        b"        self.assertEqual(client.position_manager.unassigned_position(backend.backend_id, RB), 0)\n"
    )
    updated = updated.replace(marker, assertions + marker)
expected = "c5a2debedc7f4b887c644da7d1e9c0f7c39d6ddffcf0077c03b7253057ac0f73"
if sha256(updated).hexdigest() != expected:
    raise SystemExit(f"文件还有其他差异，未写入；当前摘要={sha256(original).hexdigest()}")
if original != updated:
    backup = path.with_name(path.name + ".PF2026100902." + sha256(original).hexdigest()[:12] + ".bak")
    if not backup.exists():
        backup.write_bytes(original)
    path.write_bytes(updated)
print(f"已核验：{path} SHA256={expected}")
