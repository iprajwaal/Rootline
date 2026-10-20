import json
import subprocess
from pathlib import Path

import pytest

from rootline.agent.executor import SandboxUnavailable
from rootline.agent.kube import KubeShop, in_cluster
from rootline.catalog import load_catalog

CATALOG = load_catalog(Path(__file__).parents[1] / "corpus" / "catalog.yaml")


class FakeKubectl:
    # Records each command and answers like kubectl would.
    def __init__(self, answers: dict[str, tuple[int, str]] | None = None) -> None:
        self.answers = answers or {}
        self.ran: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        self.ran.append(argv)
        line = " ".join(argv)
        code, out = next((answer for key, answer in self.answers.items() if key in line), (0, "ok"))
        return subprocess.CompletedProcess(
            argv, code, out if code == 0 else "", out if code else ""
        )


def shop(fake: FakeKubectl, monkeypatch: pytest.MonkeyPatch, status: str = "ok") -> KubeShop:
    health: dict[str, object] = {
        "status": status,
        "components": {"db": "ok" if status == "ok" else "failing"},
    }

    class Response:
        def json(self) -> dict[str, object]:
            return health

    monkeypatch.setattr("rootline.agent.kube.httpx.get", lambda *a, **k: Response())
    return KubeShop(CATALOG, "http://shop/health", 30.0, "reader", "fixer", run=fake)


def test_in_cluster_checks_run_through_kubectl_exec() -> None:
    argv = in_cluster("cache_ping", CATALOG.checks["cache_ping"], "kind-shop")

    assert argv == [
        "kubectl", "--context", "kind-shop", "-n", "shop",
        "exec", "statefulset/cache", "--", "redis-cli", "ping",
    ]  # fmt: skip


def test_a_command_with_no_cluster_counterpart_is_refused() -> None:
    with pytest.raises(SandboxUnavailable):
        in_cluster("key_status", CATALOG.checks["key_status"], "")


def test_a_fix_is_checked_by_the_cluster_without_changing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeKubectl()

    trial = shop(fake, monkeypatch).trial("raise_pool_limit")

    assert fake.ran[-1][-1] == "--dry-run=server" and "DB_POOL_MAX=50" in fake.ran[-1]
    assert fake.ran[-1][1:3] == ["--context", "fixer"]
    assert (trial.met, trial.mode) == (True, "cluster")


def test_a_rejected_dry_run_is_not_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeKubectl({"--dry-run=server": (1, "forbidden")})

    trial = shop(fake, monkeypatch).trial("raise_pool_limit")

    assert trial.met is None and trial.note == "the cluster rejected it"


def test_find_delete_is_previewed_without_deleting(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeKubectl(
        {"find": (0, "/var/lib/shoplite/tmp/junk-1.pdf\n/var/lib/shoplite/tmp/junk-2.pdf")}
    )

    trial = shop(fake, monkeypatch).trial("clear_tmp")

    assert "-delete" not in fake.ran[-1] and fake.ran[-1][1:3] == ["--context", "reader"]
    assert trial.met and "would delete 2 files" in trial.note


def test_a_real_fix_waits_for_its_rollout_then_reads_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeKubectl({"set env": (0, "deployment.apps/orders-api env updated")})

    done = shop(fake, monkeypatch).act("raise_pool_limit")

    assert fake.ran[0][1:3] == ["--context", "fixer"]
    assert fake.ran[-1][-4:] == ["rollout", "status", "deploy/orders-api", "--timeout=180s"]
    assert done.met and done.note == "health is ok again" and done.mode == "ran"
    assert "deployment.apps/orders-api env updated" in done.output


def test_a_fix_that_leaves_a_failure_says_what_still_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    done = shop(FakeKubectl(), monkeypatch, status="degraded").act("restart_cache")

    assert done.met is False and done.note == "still failing: db"


def test_health_check_returns_the_live_health_and_logs_keep_the_newest_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = "\n".join(f"line {n}" for n in range(100))
    kube = shop(FakeKubectl({"logs": (0, lines)}), monkeypatch)

    assert json.loads(kube.check("health"))["status"] == "ok"
    logs = kube.check("logs").splitlines()
    assert logs[0] == "... 60 earlier lines" and logs[-1] == "line 99"


@pytest.mark.parametrize(("setting", "met"), [("on", True), ("off", None)])
def test_a_fix_run_inside_a_pod_is_previewed_with_its_check(
    monkeypatch: pytest.MonkeyPatch, setting: str, met: bool | None
) -> None:
    fake = FakeKubectl({"show default_transaction_read_only": (0, f"{setting}\n")})

    tried = shop(fake, monkeypatch, "degraded").trial("allow_db_writes")

    assert tried.met is met and tried.output == setting
    assert all("--context reader" in " ".join(argv) for argv in fake.ran)
    assert not any("alter system" in " ".join(argv) for argv in fake.ran)


def test_a_fix_run_inside_a_pod_waits_for_no_rollout(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeKubectl()

    done = shop(fake, monkeypatch).act("allow_db_writes")

    assert done.met is True
    assert len(fake.ran) == 1 and "--context fixer" in " ".join(fake.ran[0])
