from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_research import payload, tenant


def test_completed_zero_from_old_runtime_is_not_reused_after_fix(env, monkeypatch):
    runtime = {"app_sources_sha256": "before", "python": "3.12", "packages": {}}
    monkeypatch.setattr("app.services.research_snapshot._runtime_identity", lambda: dict(runtime))
    _, _, jobs = tenant(env)
    old = jobs.submit(payload(jobs), key="same-page-request")
    with jobs.db.business_transaction(jobs.context, write=True) as conn:
        conn.execute("UPDATE business.research_jobs SET state='succeeded' WHERE id=%s", (old["id"],))
    assert jobs.submit(payload(jobs), key="same-page-request")["id"] == old["id"]
    runtime["app_sources_sha256"] = "fixed"
    new = jobs.submit(payload(jobs), key="same-page-request")
    assert new["id"] != old["id"]
    assert jobs.get(old["id"])["state"] == "succeeded"
    assert new["metadata"]["execution_runtime"]["app_sources_sha256"] == "fixed"
