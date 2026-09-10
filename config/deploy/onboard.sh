#!/usr/bin/env bash
# Everything a NEW TENANT NAMESPACE needs, in one place. Grep anchor: DDPSRUN-ONBOARD.
#
# WHY THIS EXISTS. Adding a person took five separate steps in four different
# tools, and on 2026-09-10 two of them were found missing for a namespace that
# had existed for days: one tenant namespace had no `pacsrun-runpod` Secret at all
# (so a job there could not rent from RunPod -- the driver pod reads that key
# with a LocalObjectReference and only sees its own namespace) and its pod
# identity association still pointed at the SHARED `pacsrun-workload` role,
# which allows Put/Get/DeleteObject on `pacsrun/*` -- every other tenant's
# results. Neither failure is visible until a job runs, and one of them is a
# tenancy hole rather than an outage.
#
# END-TO-END FLOW of one `./onboard.sh ddps-newcomer`:
#
#   1. read the cluster and report what already exists       check()
#   2. namespace                                             kubectl create namespace
#   3. the workload ServiceAccount                           kubectl create serviceaccount
#   4. the gateway's namespaced Role for user secrets        rbac.yaml, substituted
#   5. the RunPod API key, copied from pacsrun-system        kubectl get -o yaml | apply
#   6. PRINT what this script must not do: the terraform
#      entry, the import command, and the token record       print_manual()
#
# ★ IT PRINTS BY DEFAULT AND ACTS ONLY WITH `--apply`, because step 5 copies a
# credential into a namespace and step 2 creates a tenancy boundary. Both are
# decisions, and a script that made them on being run once by mistake would be
# the wrong shape for either.
#
# ★ WHAT IT DELIBERATELY DOES NOT DO, and why each is somebody else's job:
#
#   the terraform entry   `pacsrun_tenants` in PACSrun's tenants.auto.tfvars,
#                         then `terraform apply`. terraform is the operator's
#                         and it does not create Kubernetes objects on purpose
#                         (variables.tf: "creating them here would make every
#                         plan depend on the cluster being reachable").
#   the association       EKS allows ONE association per (namespace,
#                         ServiceAccount), and the shared one already exists,
#                         so an apply hits 409 ResourceInUseException. It has to
#                         be imported into terraform first. This script prints
#                         the exact import line with the live association id.
#   the token record      it lives in Secrets Manager and is read by every
#                         request. Editing live auth from a shell script is not
#                         a thing this repository does.
#
#   the `RoleBinding/ddpsrun-gw` in rbac.yaml is NOT applied and does not need
#   to be: `ClusterRoleBinding/ddpsrun-gw -> Group:ddpsrun-gw` covers pacsjobs
#   cluster-wide (checked 2026-09-10). The namespaced one is left in rbac.yaml
#   for a deployment that wants to narrow that, and applying both is harmless
#   but pointless.
#
# Usage:
#   ./onboard.sh <namespace>            report only
#   ./onboard.sh <namespace> --apply    create what is missing
#
# The namespace is the one the registration mail printed. This script does NOT
# derive it from an address: `notify.namespace_suggestion` is the only
# implementation of that rule and a second one in shell would drift from it.
set -uo pipefail

NS="${1:-}"
APPLY=""
[ "${2:-}" = "--apply" ] && APPLY=1
if [ -z "$NS" ]; then
  echo "usage: $0 <namespace> [--apply]" >&2
  exit 2
fi

# Where the operator's own copies live. The Secret is read from the operator
# namespace rather than from `default`, because `default` is a tenant here too
# and reading a credential out of a tenant's namespace to seed another tenant's
# is a habit worth not starting.
SOURCE_NS="pacsrun-system"
SA="pacsjob-writer"
RUNPOD_SECRET="pacsrun-runpod"
HERE="$(cd "$(dirname "$0")" && pwd)"
CLUSTER="${DDPSRUN_CLUSTER_NAME:-pacsrun}"

say() { printf '%s\n' "$*"; }
have() { kubectl get "$1" "$2" ${3:+-n} ${3:-} >/dev/null 2>&1; }

# ---------------------------------------------------------------- step 1: read
say "== $NS =="
NS_OK=no;  kubectl get namespace "$NS"            >/dev/null 2>&1 && NS_OK=yes
SA_OK=no;  kubectl get sa "$SA" -n "$NS"          >/dev/null 2>&1 && SA_OK=yes
ROLE_OK=no; kubectl get role ddpsrun-gw-secrets -n "$NS" >/dev/null 2>&1 && ROLE_OK=yes
SEC_OK=no; kubectl get secret "$RUNPOD_SECRET" -n "$NS" >/dev/null 2>&1 && SEC_OK=yes

# The association is the half that decides whether this namespace can write S3
# at all, and WHICH prefix. Reported by role name, because "an association
# exists" is not the useful fact -- pointing at the shared role is the defect.
ASSOC_ID=""
ASSOC_ROLE=""
if command -v aws >/dev/null 2>&1; then
  ASSOC_ID=$(aws eks list-pod-identity-associations --cluster-name "$CLUSTER" \
    --namespace "$NS" --service-account "$SA" \
    --query 'associations[0].associationId' --output text 2>/dev/null)
  [ "$ASSOC_ID" = "None" ] && ASSOC_ID=""
  if [ -n "$ASSOC_ID" ]; then
    ASSOC_ROLE=$(aws eks describe-pod-identity-association --cluster-name "$CLUSTER" \
      --association-id "$ASSOC_ID" --query 'association.roleArn' --output text 2>/dev/null \
      | awk -F/ '{print $NF}')
  fi
fi

printf '  %-34s %s\n' "namespace"                    "$NS_OK"
printf '  %-34s %s\n' "serviceaccount/$SA"           "$SA_OK"
printf '  %-34s %s\n' "role/ddpsrun-gw-secrets"      "$ROLE_OK"
printf '  %-34s %s\n' "secret/$RUNPOD_SECRET"        "$SEC_OK"
printf '  %-34s %s\n' "pod identity association"     "${ASSOC_ROLE:-none}"
if [ "$ASSOC_ROLE" = "pacsrun-workload" ]; then
  say "  ★ that is the SHARED role: it allows Put/Get/DeleteObject on pacsrun/*,"
  say "    which is every tenant's results. See print_manual below."
fi
say ""

# ------------------------------------------------------- steps 2-5: the writes
run() {
  if [ -n "$APPLY" ]; then
    say "+ $*"
    "$@" || { say "  FAILED"; exit 1; }
  else
    say "  would run: $*"
  fi
}

[ "$NS_OK"  = no ] && run kubectl create namespace "$NS"
[ "$SA_OK"  = no ] && run kubectl -n "$NS" create serviceaccount "$SA"

if [ "$ROLE_OK" = no ]; then
  # The Role and its RoleBinding are the two objects in rbac.yaml carrying the
  # `<TENANT_NAMESPACE>` placeholder that this namespace needs. sed substitutes
  # and `kubectl apply` is idempotent, so re-running is safe.
  if [ -n "$APPLY" ]; then
    say "+ rbac.yaml (ddpsrun-gw-secrets) -> $NS"
    # The whole file is applied with the placeholder substituted. It also carries
    # the ClusterRole and the operator-namespace objects, and apply is idempotent,
    # so re-sending them is a no-op rather than a second definition.
    sed "s|<TENANT_NAMESPACE>|$NS|g" "$HERE/rbac.yaml" | kubectl apply -f - \
      || { say "  FAILED"; exit 1; }
  else
    say "  would run: sed 's|<TENANT_NAMESPACE>|$NS|g' rbac.yaml | kubectl apply -f -"
  fi
fi

if [ "$SEC_OK" = no ]; then
  # ★ THE COPY, AND WHY IT IS A COPY AND NOT A REFERENCE. The driver pod names
  # this Secret with a LocalObjectReference (internal/controller/vendorpod.go),
  # which resolves in the pod's OWN namespace and nowhere else. There is no
  # cluster-scoped Secret in Kubernetes, so every namespace that may rent from
  # RunPod needs its own copy. `--export` was removed from kubectl in 1.18, so
  # the metadata is stripped here instead: keeping resourceVersion or the old
  # namespace makes apply refuse.
  if [ -n "$APPLY" ]; then
    say "+ copy secret/$RUNPOD_SECRET from $SOURCE_NS -> $NS"
    kubectl -n "$SOURCE_NS" get secret "$RUNPOD_SECRET" -o json \
      | python3 -c 'import json,sys; d=json.load(sys.stdin); m=d["metadata"]; d["metadata"]={"name":m["name"]}; print(json.dumps(d))' \
      | kubectl -n "$NS" apply -f - || { say "  FAILED"; exit 1; }
  else
    say "  would run: kubectl -n $SOURCE_NS get secret $RUNPOD_SECRET -o json | <strip metadata> | kubectl -n $NS apply -f -"
    say "             (this copies a vendor API KEY: it lets this namespace spend money)"
  fi
fi

# ----------------------------------------------------- step 6: what stays manual
say ""
say "-- not this script's to do --"
say ""
say "1. PACSrun terraform/cluster/tenants.auto.tfvars:"
say "     \"$NS\" = { team = \"<team>\", service_account = \"$SA\" }"
if [ -n "$ASSOC_ID" ] && [ "$ASSOC_ROLE" = "pacsrun-workload" ]; then
  say ""
  say "2. import the association FIRST, or apply hits 409 ResourceInUseException:"
  say "     terraform import 'aws_eks_pod_identity_association.tenant[\"$NS\"]' \\"
  say "       $CLUSTER,$ASSOC_ID"
  say ""
  say "3. terraform plan   # WITHOUT -target, so nothing stays pending"
  say "   terraform apply  # moves this namespace onto pacsrun-tenant-$NS,"
  say "                    # which allows only pacsrun/$NS/* and has no DeleteObject"
else
  say ""
  say "2. terraform plan (WITHOUT -target) then apply"
fi
say ""
say "4. the token record, in Secrets Manager (DDPSRUN_TOKENS_SECRET_ID):"
say "     {\"email\": \"<address>\", \"user\": \"<local part>\", \"namespace\": \"$NS\", \"team\": \"<team>\"}"
say "   The registration mail prints this filled in -- paste that rather than typing it."
say ""
[ -z "$APPLY" ] && say "(nothing was changed. re-run with --apply)"
exit 0
