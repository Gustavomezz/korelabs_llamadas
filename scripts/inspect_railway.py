#!/usr/bin/env python3
"""
Inspect Railway projects — READ ONLY.

No modifica nada. Solo lista proyectos, environments, servicios, su tipo
(GitHub/Docker/Postgres), URLs, branches conectadas y NOMBRES de env vars
(NO los valores, para no exponer secretos).

Uso:
    export RAILWAY_API_TOKEN="..."
    python3 scripts/inspect_railway.py > railway_inventory.txt

Pegame el contenido de railway_inventory.txt y con eso armo el script
de modificaciones.

Si una query falla (porque Railway cambió el schema), el script lo loguea
pero sigue con las demás. Output siempre legible.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ENDPOINT = "https://backboard.railway.com/graphql/v2"


def gql(query: str, variables: dict | None = None) -> dict:
    """POST a GraphQL query via curl (evita problemas SSL de Python en macOS).

    Returns dict con la respuesta o {"_http_error": "..."} si falló.
    """
    token = os.environ.get("RAILWAY_API_TOKEN")
    if not token:
        sys.exit("ERROR: RAILWAY_API_TOKEN env var no seteada")

    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    try:
        result = subprocess.run(
            [
                "curl", "-sS", "--max-time", "30",
                "-X", "POST", ENDPOINT,
                "-H", f"Authorization: Bearer {token}",
                "-H", "Content-Type: application/json",
                "-H", "User-Agent: korelabs-inspect/1.0",
                "--data-binary", json.dumps(payload),
            ],
            capture_output=True,
            text=True,
            timeout=35,
        )
    except subprocess.TimeoutExpired:
        return {"_http_error": "curl timeout (>35s)"}
    except FileNotFoundError:
        return {"_http_error": "curl no encontrado en PATH"}

    if result.returncode != 0:
        return {"_http_error": f"curl exit {result.returncode}: {result.stderr[:300]}"}

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"_http_error": f"respuesta no es JSON: {result.stdout[:300]}"}


def safe_print(msg=""):
    print(msg, flush=True)


def mask_value(value: str) -> str:
    """Para mostrar SOLO el length y formato de un valor. Nunca el valor real."""
    s = str(value) if value is not None else ""
    n = len(s)
    if n == 0:
        return "(empty)"
    # Detectar si parece URL para mostrar solo el host
    if s.startswith(("http://", "https://", "postgres://", "postgresql://")):
        try:
            scheme, rest = s.split("://", 1)
            host = rest.split("@")[-1].split("/")[0].split(":")[0]
            return f"<{scheme} → {host}> ({n} chars)"
        except Exception:
            return f"<URL-like> ({n} chars)"
    # Detectar booleanos y números simples
    if s in ("true", "false", "True", "False"):
        return f"={s} ({n} chars)"
    if s.isdigit() and n < 12:
        return f"={s} ({n} chars)"
    return f"({n} chars)"


def section(title: str):
    safe_print()
    safe_print("=" * 72)
    safe_print(title)
    safe_print("=" * 72)


# ──────────────────────────────────────────────────────────────────────────


def main():
    # ── 0. Health check ───────────────────────────────────────────────────
    section("0. CONECTIVIDAD")

    # Intentar `me` (solo funciona con Personal Access Token)
    me = gql("query { me { id name email } }")
    if "_http_error" in me:
        sys.exit(f"FATAL HTTP: {me['_http_error']}")

    if "errors" not in me:
        me_data = me.get("data", {}).get("me") or {}
        safe_print(f"Connected as: {me_data.get('name', '?')} <{me_data.get('email', '?')}>")
        safe_print(f"User id: {me_data.get('id', '?')}")
        safe_print("Token type: Personal Access Token (full scope)")
    else:
        err_msg = me["errors"][0].get("message", "?")
        safe_print(f"Query 'me' falló: {err_msg}")
        safe_print("→ El token NO es un Personal Access Token. Probablemente es Project/Team Token.")
        safe_print("→ Intentando listar proyectos de todas formas...")
        safe_print()

    # Intentar listar proyectos genéricamente — funciona con PAT
    test = gql("query { projects { edges { node { id name } } } }")
    if "errors" in test:
        err_msg = test["errors"][0].get("message", "?")
        safe_print(f"Query 'projects' (genérica) también falló: {err_msg}")
        safe_print("→ Esto sugiere que el token es Project Token (atado a un solo proyecto).")
        safe_print()
        safe_print("Intentando descubrir el proyecto vinculado vía introspection...")
        # Con un project token podemos usar projectToken { projectId } o similar
        pt_test = gql("query { projectToken { projectId environmentId } }")
        if "errors" not in pt_test:
            pt = pt_test.get("data", {}).get("projectToken") or {}
            safe_print(f"  projectId: {pt.get('projectId', '?')}")
            safe_print(f"  environmentId: {pt.get('environmentId', '?')}")
            safe_print()
            safe_print("ACCIÓN REQUERIDA: este token solo accede a UN proyecto.")
            safe_print("Para inspeccionar los 4 proyectos necesitamos un Personal Access Token.")
            safe_print("  1. Railway → tu avatar → Account Settings → Tokens")
            safe_print("  2. Generate Personal Token (no Team, no Project)")
            safe_print("  3. Reemplazar RAILWAY_API_TOKEN y volver a correr este script")
            sys.exit(2)
        else:
            safe_print(f"  introspection projectToken también falló: {pt_test['errors'][0].get('message', '?')}")
            safe_print()
            safe_print("ACCIÓN REQUERIDA: regenerar el token como Personal Access Token.")
            safe_print("  1. Railway → tu avatar → Account Settings → Tokens")
            safe_print("  2. Generate Personal Token")
            sys.exit(2)

    # ── 1. Projects (with envs + services) ────────────────────────────────
    section("1. PROYECTOS Y ESTRUCTURA")

    projects_query = """
    query {
      projects {
        edges {
          node {
            id
            name
            description
            createdAt
            isPublic
            team { id name }
            environments {
              edges {
                node {
                  id
                  name
                  isEphemeral
                }
              }
            }
            services {
              edges {
                node {
                  id
                  name
                  createdAt
                }
              }
            }
          }
        }
      }
    }
    """
    result = gql(projects_query)
    if "_http_error" in result:
        sys.exit(f"FATAL listing projects: {result['_http_error']}")
    if "errors" in result:
        safe_print("GraphQL errors al listar proyectos:")
        safe_print(json.dumps(result["errors"], indent=2))
        sys.exit(1)

    projects = result.get("data", {}).get("projects", {}).get("edges", [])
    safe_print(f"Total proyectos: {len(projects)}\n")

    # Mostrar resumen
    for edge in projects:
        p = edge["node"]
        envs = p.get("environments", {}).get("edges", [])
        services = p.get("services", {}).get("edges", [])
        safe_print(f"• {p['name']}")
        safe_print(f"    id: {p['id']}")
        safe_print(f"    creado: {p.get('createdAt', '?')[:10]}")
        if p.get("description"):
            safe_print(f"    descripción: {p['description']}")
        if p.get("team"):
            safe_print(f"    team: {p['team']['name']}")
        safe_print(f"    public: {p.get('isPublic', False)}")
        safe_print(f"    environments: {[e['node']['name'] for e in envs]}")
        safe_print(f"    services ({len(services)}): {[s['node']['name'] for s in services]}")
        safe_print()

    # ── 2. Service details (source, image, GitHub repo) ───────────────────
    section("2. DETALLE DE CADA SERVICIO")

    # Query a service individually to get source info
    service_detail_query = """
    query ServiceDetail($id: String!) {
      service(id: $id) {
        id
        name
        projectId
        templateServiceId
        deletedAt
        repoTriggers {
          edges {
            node {
              id
              repository
              branch
              environmentId
            }
          }
        }
      }
    }
    """

    for edge in projects:
        p = edge["node"]
        services = p.get("services", {}).get("edges", [])
        if not services:
            continue
        safe_print(f"── Proyecto: {p['name']} ──")
        for s_edge in services:
            s = s_edge["node"]
            safe_print(f"  Service: {s['name']}")
            safe_print(f"    id: {s['id']}")
            detail = gql(service_detail_query, {"id": s["id"]})
            if "errors" in detail:
                safe_print(f"    (no se pudo obtener detalle: {detail['errors'][0].get('message', '?')})")
                continue
            sd = detail.get("data", {}).get("service") or {}
            triggers = sd.get("repoTriggers", {}).get("edges", []) if sd else []
            if triggers:
                safe_print(f"    GitHub triggers:")
                for t in triggers:
                    tn = t["node"]
                    safe_print(f"      - repo: {tn.get('repository', '?')}  branch: {tn.get('branch', '?')}")
            if sd.get("templateServiceId"):
                safe_print(f"    templateServiceId: {sd['templateServiceId']} (probablemente Postgres/Redis/etc)")
        safe_print()

    # ── 3. Deployments (latest per service+env) ──────────────────────────
    section("3. ÚLTIMO DEPLOYMENT POR SERVICIO/ENV")

    deployments_query = """
    query Deployments($projectId: String!) {
      deployments(
        first: 50
        input: { projectId: $projectId, status: { in: [SUCCESS, FAILED, BUILDING, DEPLOYING, CRASHED] } }
      ) {
        edges {
          node {
            id
            status
            createdAt
            staticUrl
            url
            serviceId
            environmentId
            meta
          }
        }
      }
    }
    """

    for edge in projects:
        p = edge["node"]
        services_by_id = {s["node"]["id"]: s["node"]["name"] for s in p.get("services", {}).get("edges", [])}
        envs_by_id = {e["node"]["id"]: e["node"]["name"] for e in p.get("environments", {}).get("edges", [])}
        if not services_by_id:
            continue

        result = gql(deployments_query, {"projectId": p["id"]})
        if "errors" in result:
            safe_print(f"── {p['name']}: error listando deployments ({result['errors'][0].get('message', '?')})")
            continue

        deps = result.get("data", {}).get("deployments", {}).get("edges", [])
        # Quedarme con el más reciente por (service, env)
        latest: dict[tuple[str, str], dict] = {}
        for d_edge in deps:
            d = d_edge["node"]
            key = (d.get("serviceId"), d.get("environmentId"))
            if key not in latest:
                latest[key] = d

        safe_print(f"── Proyecto: {p['name']} ──")
        if not latest:
            safe_print("  (sin deployments listados)")
        for (svc_id, env_id), d in latest.items():
            svc_name = services_by_id.get(svc_id, svc_id[:8] if svc_id else "?")
            env_name = envs_by_id.get(env_id, env_id[:8] if env_id else "?")
            safe_print(f"  {svc_name} [{env_name}]")
            safe_print(f"    status: {d.get('status', '?')}")
            safe_print(f"    fecha: {d.get('createdAt', '?')[:19]}")
            if d.get("staticUrl"):
                safe_print(f"    staticUrl: {d['staticUrl']}")
            if d.get("url"):
                safe_print(f"    url: {d['url']}")
            meta = d.get("meta")
            if isinstance(meta, dict) and meta:
                interesting = {k: v for k, v in meta.items() if k in ("commitHash", "commitMessage", "repo", "branch")}
                if interesting:
                    safe_print(f"    meta: {interesting}")
        safe_print()

    # ── 4. Env var NAMES (no values) ─────────────────────────────────────
    section("4. NOMBRES DE ENV VARS (no se imprimen valores)")

    variables_query = """
    query Variables($projectId: String!, $environmentId: String!, $serviceId: String) {
      variables(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId)
    }
    """

    for edge in projects:
        p = edge["node"]
        envs = p.get("environments", {}).get("edges", [])
        services = p.get("services", {}).get("edges", [])
        if not envs:
            continue

        # Preferir production
        prod_env = next(
            (e["node"] for e in envs if e["node"]["name"] == "production"),
            envs[0]["node"] if envs else None,
        )
        if not prod_env:
            continue

        safe_print(f"── Proyecto: {p['name']}  (env: {prod_env['name']}) ──")

        # Project-level variables (shared)
        proj_vars = gql(variables_query, {
            "projectId": p["id"],
            "environmentId": prod_env["id"],
            "serviceId": None,
        })
        if "errors" not in proj_vars:
            v = proj_vars.get("data", {}).get("variables") or {}
            if v:
                safe_print(f"  Project-level (shared):")
                for name in sorted(v.keys()):
                    safe_print(f"    - {name}  {mask_value(v[name])}")

        # Service-level variables
        for s_edge in services:
            s = s_edge["node"]
            svc_vars = gql(variables_query, {
                "projectId": p["id"],
                "environmentId": prod_env["id"],
                "serviceId": s["id"],
            })
            if "errors" in svc_vars:
                safe_print(f"  Service '{s['name']}': error ({svc_vars['errors'][0].get('message', '?')[:80]})")
                continue
            v = svc_vars.get("data", {}).get("variables") or {}
            if v:
                safe_print(f"  Service '{s['name']}':")
                for name in sorted(v.keys()):
                    safe_print(f"    - {name}  {mask_value(v[name])}")
        safe_print()

    # ── 5. Domains públicos ──────────────────────────────────────────────
    section("5. DOMINIOS PÚBLICOS POR SERVICIO")

    # Para cada (project, environment, service), intentar listar domains
    domains_query = """
    query Domains($projectId: String!, $environmentId: String!, $serviceId: String!) {
      domains(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId) {
        serviceDomains {
          id
          domain
        }
        customDomains {
          id
          domain
          status
        }
      }
    }
    """

    for edge in projects:
        p = edge["node"]
        envs = p.get("environments", {}).get("edges", [])
        services = p.get("services", {}).get("edges", [])
        if not envs or not services:
            continue
        prod_env = next(
            (e["node"] for e in envs if e["node"]["name"] == "production"),
            envs[0]["node"] if envs else None,
        )
        if not prod_env:
            continue

        safe_print(f"── Proyecto: {p['name']} ──")
        for s_edge in services:
            s = s_edge["node"]
            d = gql(domains_query, {
                "projectId": p["id"],
                "environmentId": prod_env["id"],
                "serviceId": s["id"],
            })
            if "errors" in d:
                continue
            doms = d.get("data", {}).get("domains") or {}
            svc_doms = doms.get("serviceDomains") or []
            cust_doms = doms.get("customDomains") or []
            if svc_doms or cust_doms:
                safe_print(f"  Service '{s['name']}':")
                for sd in svc_doms:
                    safe_print(f"    - {sd.get('domain', '?')}  (railway)")
                for cd in cust_doms:
                    safe_print(f"    - {cd.get('domain', '?')}  (custom, status={cd.get('status', '?')})")
        safe_print()

    section("FIN DEL INVENTARIO")
    safe_print()
    safe_print("Si todo se ve raro o vacío, posibles causas:")
    safe_print("  - Railway cambió su GraphQL schema (algunos campos arriba pueden fallar)")
    safe_print("  - El token no tiene permisos suficientes (team / project scope)")
    safe_print("  - Hay más proyectos en otros teams no accesibles con este token")


if __name__ == "__main__":
    main()
