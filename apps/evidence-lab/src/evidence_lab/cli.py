"""Explicit-config operator commands. Nothing invokes a local model runtime."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import mimetypes
import signal
import subprocess
import sys
import uuid
from pathlib import Path

from evidence_lab.api import create_app, make_store, public_job, public_run
from evidence_lab.config import ConfigError, load_config
from evidence_lab.domain import CallContext, EvidenceItem, EvidencePack, ProviderError, strict_json
from evidence_lab.ingestion import admit_document, pipeline_revision
from evidence_lab.policy import evaluate_checks, policy_state, semantic_policy_fingerprint
from evidence_lab.providers import ProviderHub
from evidence_lab.retrieval import retrieve_evidence, space_manifest
from evidence_lab.worker import Worker


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def parser():
    cli = argparse.ArgumentParser(prog="evidence-lab", description="Evidence Lab: document-grounded retrieval and verification")
    cli.add_argument("--config", default="configs/mock.yaml", help="Private YAML configuration path")
    sub = cli.add_subparsers(dest="command", required=True)

    def command(name, help):
        result = sub.add_parser(name, help=help)
        result.add_argument("--config", default=argparse.SUPPRESS, help="Private YAML configuration path")
        return result

    command("config-check", "Validate and display redacted configuration")
    command("migrate", "Apply explicit database migrations and initialize the default corpus")
    server = command("serve", "Start the HTTP API and operator UI")
    server.add_argument("--host", help="Optional bind-address override")
    server.add_argument("--port", type=int, help="Optional port override")
    worker = command("worker", "Process durable jobs")
    worker.add_argument("--once", action="store_true", help="Process at most one job and exit")
    worker.add_argument("--concurrency", type=int, help="Override job concurrency; outbound cap still applies")
    seed = command("seed", "Upload the bundled synthetic documents through ordinary ingestion")
    seed.add_argument("--wait", action="store_true", help="Run queued jobs locally until seed jobs finish")
    upload = command("ingest", "Enqueue a local text, Markdown or PDF document")
    upload.add_argument("path", type=Path)
    upload.add_argument("--corpus", default="default")
    upload.add_argument("--document-id", help="Create a new immutable version of this document")
    upload.add_argument("--wait", action="store_true")
    query = command("query", "Enqueue a document-grounded question")
    query.add_argument("question")
    query.add_argument("--corpus", default="default")
    query.add_argument("--wait", action="store_true")
    retrieval = command("retrieve", "Preview hybrid evidence without answer generation")
    retrieval.add_argument("question")
    retrieval.add_argument("--corpus", default="default")
    smoke = command("smoke", "Plan or execute minimal configured endpoint contract checks")
    smoke.add_argument("--execute", action="store_true", help="Make the budgeted calls (default is dry run)")
    evaluation = command("evaluate", "Estimate or execute an operator-selected evaluation dataset")
    evaluation.add_argument("--dataset", type=Path, default=Path("data/demo/dataset.json"))
    evaluation.add_argument("--split", choices=["all", "development", "test", "demo"])
    evaluation.add_argument("--annotations", type=Path)
    evaluation.add_argument("--output", type=Path, default=Path("artifacts/evaluation"))
    evaluation.add_argument("--execute", action="store_true")
    export = command("eval-export", "Export a stored evaluation job")
    export.add_argument("job_id")
    export.add_argument("--output", type=Path, default=Path("artifacts/evaluation"))
    review = command("eval-review", "Apply human annotations to an existing report, without inference")
    review.add_argument("report", type=Path)
    review.add_argument("--annotations", type=Path, required=True)
    review.add_argument("--output", type=Path, default=Path("artifacts/reviewed-evaluation"))
    qualify = command("qualify", "Assess frozen empirical, fault, load and repeatability evidence")
    qualify.add_argument("report", type=Path)
    qualify.add_argument("--fault-artifact", type=Path, required=True)
    qualify.add_argument("--load-artifact", type=Path, required=True)
    qualify.add_argument("--repeatability-artifact", type=Path, required=True)
    qualify.add_argument("--policy-id", required=True)
    qualify.add_argument("--output", type=Path, default=Path("policies/candidate.json"))
    experiments = command("experiments", "Predeclare and run repeatability/load studies (see module help)")
    experiments.add_argument("arguments", nargs=argparse.REMAINDER)
    inspect = command("inspect", "Read a job or query run")
    inspect.add_argument("id")
    inspect.add_argument("--run", action="store_true")
    inspect.add_argument("--trace", action="store_true", help="Include explicitly unverified operator trace")
    command("cleanup", "Apply configured retention while preserving retained-run source references")
    command("policy-fingerprint", "Print the semantic identity required by a release policy")
    command("offline-tests", "Run local contract tests without live inference")
    return cli


async def wait_job(job_id, store, hub, config):
    worker = Worker(store, hub, config, concurrency=1)
    while True:
        current = store.get_job(job_id)
        if current["status"] not in {"queued", "running"}:
            return public_job(current)
        result = await worker.run_once()
        if result is None:
            await asyncio.sleep(config.runtime.poll_seconds)


async def smoke_check(config, store, hub):
    text = "The reference project is named Atlas. Its support window is 09:00 to 17:00 UTC."
    evidence = EvidencePack(corpus_id="smoke-fixture", space_id="contract-only", corpus_revision=1,
        items=[EvidenceItem(id="smoke-e1", document_id="smoke-doc", version_id="smoke-v1",
                            title="Endpoint contract fixture", text=text, page=1, start=0, end=len(text),
                            text_hash=hashlib.sha256(text.encode()).hexdigest())])
    ctx = CallContext.for_seconds("smoke:" + str(uuid.uuid4()), "smoke",
                                  config.runtime.query_deadline_seconds,
                                  config.runtime.max_remote_attempts_per_query)
    vectors = await hub.embed([text], ctx)
    question = "What is the project named, and what is its support window?"
    draft = await hub.generate(question, evidence, ctx)
    verdict = await hub.verify(question, draft, evidence, ctx, round_id="smoke")
    gate = evaluate_checks(draft, evidence, verdict, config.verification.score_threshold,
                           expected_round_id="smoke")
    return {"mode": config.runtime.mode, "run_id": ctx.run_id, "dimensions": len(vectors[0]),
            "gate": gate, "passed": gate["accepted"], "qualification": "contract_only",
            "note": "Endpoint shape check only; this does not measure domain accuracy.",
            "calls": store.get_calls(ctx.run_id)}


def export_evaluation(report, output):
    from evidence_lab.evaluation import annotation_template, export_report
    files = export_report(report, output)
    template = Path(output) / "answer-review-template.json"
    template.write_text(json.dumps(annotation_template(report), ensure_ascii=False, indent=2,
                                    allow_nan=False) + "\n", encoding="utf-8")
    files["annotation_template"] = str(template)
    return files


async def run_command(args, config):
    store = make_store(config)
    if args.command == "qualify":
        from evidence_lab.evaluation import qualify_policy
        report = strict_json(args.report.read_text(encoding="utf-8"))
        policy = qualify_policy(report, config, fault_artifact=args.fault_artifact,
                                load_artifact=args.load_artifact,
                                repeatability_artifact=args.repeatability_artifact, policy_id=args.policy_id)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Never overwrite a previously frozen policy by accident.
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(policy, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
        emit({"qualified": policy["qualified"], "policy_file": str(args.output),
              "unmet_requirements": policy["unmet_requirements"]})
        if not policy["qualified"]:
            raise SystemExit(2)
        return
    if args.command == "migrate":
        store.migrate()
        manifest = space_manifest(config)
        try:
            corpus = store.ensure_corpus("default", manifest)
        except ProviderError as exc:
            if exc.status != "space_changed":
                raise
            corpus = store.get_corpus("default")
            if corpus["space_id"] == manifest["id"]:
                raise
            # Schema migration must still allow the UI to inspect an older
            # corpus and create a new one after an explicit space change.
        emit({"status": "migrated", "corpus": corpus,
              "embedding_space_matches": corpus["space_id"] == manifest["id"]})
        return
    if args.command == "inspect":
        if args.run:
            value = store.get_run(args.id)
            if args.trace:
                emit({"unverified": True, "label": "Operator diagnostics", "run": value,
                      "calls": store.get_calls(args.id)})
            else:
                emit(public_run(value))
        else:
            emit(public_job(store.get_job(args.id)))
        return
    if args.command == "cleanup":
        emit(store.cleanup())
        return
    if args.command == "eval-export":
        job = store.get_job(args.job_id)
        if job["kind"] != "evaluation" or not job.get("result"):
            raise ProviderError("not_found", "No completed evaluation report is available for this job.")
        emit(export_evaluation(job["result"], args.output))
        return
    if args.command == "eval-review":
        from evidence_lab.evaluation import apply_annotations
        emit(export_evaluation(apply_annotations(args.report, args.annotations), args.output))
        return
    if args.command == "smoke" and not args.execute:
        emit({"dry_run": True, "mode": config.runtime.mode,
              "logical_calls": {"embedding": 1, "generation": 1, "verification_batch": 1},
              "maximum_attempts": 6, "phase": "smoke", "budget": config.budgets.model_dump(mode="json"),
              "note": "Use --execute after declaring active profile prices and a positive live smoke budget."})
        return
    if args.command == "evaluate" and not args.execute:
        from evidence_lab.evaluation import estimate_study, load_dataset
        dataset = load_dataset(args.dataset)
        selection = args.split or ("demo" if dataset.purpose == "synthetic_fixture" else "test")
        splits = ["development", "test", "demo"] if selection == "all" else [selection]
        emit(estimate_study(dataset, config, splits))
        return

    hub = ProviderHub(config, store=store)
    try:
        if args.command == "worker":
            worker = Worker(store, hub, config, concurrency=args.concurrency)
            if args.once:
                emit(await worker.run_once())
            else:
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(sig, worker.stop)
                await worker.run_forever()
        elif args.command in {"ingest", "seed"}:
            if args.command == "seed":
                from evidence_lab.evaluation import demo_documents
                if config.runtime.mode != "mock":
                    raise ProviderError("invalid_data", "The bundled demonstration seed is for mock mode.")
                documents = demo_documents(config)
                corpus_id, document_id = "default", None
            else:
                with args.path.open("rb") as handle:
                    raw = handle.read(config.ingestion.max_upload_bytes + 1)
                documents = [{"name": args.path.name, "raw": raw,
                              "media_type": mimetypes.guess_type(args.path.name)[0] or "application/octet-stream"}]
                corpus_id, document_id = args.corpus, args.document_id
            store.ensure_corpus(corpus_id, space_manifest(config))
            uploads = []
            for doc in documents:
                admit_document(doc["raw"], doc["name"], doc["media_type"], config)
                result = store.create_document(doc["name"], doc["raw"], doc["media_type"], corpus_id,
                                               document_id, pipeline_revision(config))
                if args.wait:
                    result["job"] = await wait_job(result["job_id"], store, hub, config)
                uploads.append(result)
            emit({"documents": uploads, "mode": config.runtime.mode})
        elif args.command == "query":
            value = store.create_run(args.question, args.corpus, {"config_fingerprint": config.fingerprint()})
            if args.wait:
                await wait_job(value["job_id"], store, hub, config)
                value = store.get_run(value["id"])
            emit(public_run(value))
        elif args.command == "retrieve":
            ctx = CallContext.for_seconds("preview:" + str(uuid.uuid4()), "queries",
                                          config.runtime.query_deadline_seconds,
                                          config.runtime.max_remote_attempts_per_query)
            pack = await retrieve_evidence(args.question, args.corpus, store, hub, config, ctx)
            emit({"evidence": pack.model_dump(mode="json"), "mode": config.runtime.mode})
        elif args.command == "smoke":
            result = await smoke_check(config, store, hub)
            emit(result)
            if not result["passed"]:
                raise SystemExit(2)
        elif args.command == "evaluate":
            job = store.enqueue_job("evaluation", {
                "operator_dataset_path": str(args.dataset.resolve()), "split": args.split,
                "annotations_path": str(args.annotations.resolve()) if args.annotations else None,
                "config_fingerprint": config.fingerprint(),
            })
            completed = await wait_job(job["id"], store, hub, config)
            if not completed.get("result"):
                emit(completed)
                raise SystemExit(2)
            emit({"job_id": job["id"], "status": completed["status"],
                  "files": export_evaluation(completed["result"], args.output)})
    finally:
        await hub.aclose()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "config-check":
            emit({"configuration": config.safe_dict(), "fingerprint": config.fingerprint(),
                  "policy": policy_state(config)})
        elif args.command == "policy-fingerprint":
            emit({"semantic_fingerprint": semantic_policy_fingerprint(config), "policy": policy_state(config)})
        elif args.command == "offline-tests":
            raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "-q", "-m",
                                             "not integration and not native_postgres"]))
        elif args.command == "experiments":
            from evidence_lab.experiments import main as experiments_main
            forwarded = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
            raise SystemExit(experiments_main(["--config", args.config, *forwarded]))
        elif args.command == "serve":
            import uvicorn
            uvicorn.run(create_app(config), host=args.host or config.runtime.host,
                        port=args.port or config.runtime.port, log_level="warning")
        else:
            asyncio.run(run_command(args, config))
    except (ConfigError, ProviderError) as exc:
        emit({"error": str(exc), "code": getattr(exc, "status", "invalid_configuration")})
        raise SystemExit(2) from None
    except (OSError, ValueError):
        emit({"error": "The command input or selected file is invalid.", "code": "invalid_input"})
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
