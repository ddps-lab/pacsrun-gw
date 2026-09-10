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
#   5. EVERY vendor credential, copied from pacsrun-system   kubectl get -o json | apply
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

# ★ EVERY VENDOR CREDENTIAL A DRIVER POD MAY NEED, and why this is a list rather
# than one name. A driver pod names its vendor key with a LocalObjectReference
# (PACSrun internal/controller/vendorpod.go:1440 for RunPod,
# gcpdriverpod.go:823 for GCP), which resolves in the POD'S OWN namespace and
# nowhere else. Kubernetes has no cluster-scoped Secret, so a namespace that may
# rent from a vendor needs that vendor's key sitting in it.
#
# Missing one is invisible until a job runs: the pod stops at
# CreateContainerConfigError and kubelet says `secret "pacsrun-gcp" not found`.
# Measured 2026-09-10 across the live cluster -- every tenant namespace had
# pacsrun-runpod and NONE had pacsrun-gcp or pacsrun-shadeform, so a GCP or
# Shadeform job in a tenant namespace could not have started.
#
# AWS IS DELIBERATELY ABSENT. The AWS driver authenticates to its vendor with
# the projected Kubernetes token the pod already mounts plus a role ARN, so
# there is no Secret object anywhere on that path
# (PACSrun internal/controller/awsdriverpod.go:472-480).
#
# ONE KEY PER VENDOR, SHARED BY EVERY TENANT. Verified 2026-09-10: the four
# copies of pacsrun-runpod on this cluster are byte-identical (same sha256).
# S3 is split per tenant by IAM prefix; the VENDOR ACCOUNT is not split, so a
# namespace holding this key can see and delete every pod on that account,
# including another researcher's. That is a property of the account, not of
# this script, and it is why the copy is printed before it is made.
VENDOR_SECRETS="pacsrun-runpod pacsrun-shadeform pacsrun-gcp"
HERE="$(cd "$(dirname "$0")" && pwd)"
CLUSTER="${DDPSRUN_CLUSTER_NAME:-pacsrun}"

say() { printf '%s\n' "$*"; }
have() { kubectl get "$1" "$2" ${3:+-n} ${3:-} >/dev/null 2>&1; }

# ---------------------------------------------------------------- step 1: read
say "== $NS =="
NS_OK=no;  kubectl get namespace "$NS"            >/dev/null 2>&1 && NS_OK=yes
SA_OK=no;  kubectl get sa "$SA" -n "$NS"          >/dev/null 2>&1 && SA_OK=yes
ROLE_OK=no; kubectl get role ddpsrun-gw-secrets -n "$NS" >/dev/null 2>&1 && ROLE_OK=yes
# MISSING_SECRETS is what this namespace lacks AND the operator namespace has, so
# it can be copied. NO_SOURCE is what neither has -- reported, never invented.
MISSING_SECRETS=""
NO_SOURCE=""
for vs in $VENDOR_SECRETS; do
  kubectl get secret "$vs" -n "$NS" >/dev/null 2>&1 && continue
  if kubectl get secret "$vs" -n "$SOURCE_NS" >/dev/null 2>&1; then
    MISSING_SECRETS="$MISSING_SECRETS $vs"
  else
    NO_SOURCE="$NO_SOURCE $vs"
  fi
done

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
for vs in $VENDOR_SECRETS; do
  if kubectl get secret "$vs" -n "$NS" >/dev/null 2>&1; then
    printf '  %-34s %s\n' "secret/$vs" "yes"
  elif kubectl get secret "$vs" -n "$SOURCE_NS" >/dev/null 2>&1; then
    printf '  %-34s %s\n' "secret/$vs" "no (copyable from $SOURCE_NS)"
  else
    printf '  %-34s %s\n' "secret/$vs" "no (and none in $SOURCE_NS either)"
  fi
done
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

for vs in $MISSING_SECRETS; do
  # ★ THE COPY, AND WHY IT IS A COPY AND NOT A REFERENCE. See VENDOR_SECRETS
  # above for the LocalObjectReference argument. `--export` was removed from
  # kubectl in 1.18, so the metadata is stripped here instead: keeping
  # resourceVersion or the old namespace makes apply refuse.
  #
  # ONLY name SURVIVES the strip. Dropping labels and annotations along with it
  # is deliberate -- an annotation like kubectl.kubernetes.io/last-applied
  # carries the SOURCE namespace's own apply record, and a
  # `kubernetes.io/service-account.name` would bind the copy to a
  # ServiceAccount that does not exist here. `type` and `data` are NOT touched:
  # a Secret's type is part of what it is, and rewriting it to Opaque would
  # silently change the object for any vendor whose key is not Opaque.
  if [ -n "$APPLY" ]; then
    say "+ copy secret/$vs from $SOURCE_NS -> $NS"
    kubectl -n "$SOURCE_NS" get secret "$vs" -o json \
      | python3 -c 'import json,sys; d=json.load(sys.stdin); d["metadata"]={"name":d["metadata"]["name"]}; print(json.dumps(d))' \
      | kubectl -n "$NS" apply -f - || { say "  FAILED"; exit 1; }
  else
    say "  would run: kubectl -n $SOURCE_NS get secret $vs -o json | <strip metadata> | kubectl -n $NS apply -f -"
    say "             (this copies a vendor API KEY: it lets this namespace spend money on that account)"
  fi
done

for vs in $NO_SOURCE; do
  # Reported and not created. A key this cluster does not have is one somebody
  # has to obtain from the vendor, and inventing an empty Secret here would turn
  # a legible `not found` into a vendor 401 several seconds and one API call later.
  say "  ! secret/$vs is missing here AND in $SOURCE_NS -- nothing to copy."
  say "    a job asking for that vendor in $NS will stop at CreateContainerConfigError."
done

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
