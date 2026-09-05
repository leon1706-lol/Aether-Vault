# Runbook: onboarding a new tenant

See [`docs/enterprise-operator-guide.md`](../enterprise-operator-guide.md) for the full
walkthrough this runbook summarizes into an operational checklist.

## 1. Provision the tenant

```bash
av tenant create <slug> "<Display Name>"
```

Requires an `admin`-scoped credential. Idempotent by slug — re-running against an
existing slug reports "already exists" rather than creating a duplicate.

## 2. Create the tenant's first admin user

```bash
av user create <username> --email <email> --display-name "<Name>"
av role grant user <user-id> admin
```

## 3. Mint that tenant's first token (for CI/automation, not a human)

```bash
av token create <name> --scope improver:write --expires-in-days 90
```

Hand the printed token to the tenant over a secure channel — it is shown exactly once.

## 4. Decide on tenant enforcement

If `AV_TENANCY_ENFORCE` is not already `1` on this registry, onboarding a SECOND real
tenant is exactly the moment to turn it on — an unenforced registry with 2+ tenants means
neither is actually isolated from the other. See `development/architecture.md`'s Tenancy
Isolation Contract before flipping this on a registry with existing production data:
confirm `AV_APP_DATABASE_URL` is also set (otherwise the RLS backstop layer is inert —
see that same contract section, and `development/threat-model.md` T14).

## 5. Verify isolation before calling it done

```bash
av tenant show   # using the NEW tenant's own token — confirms it resolves to the right tenant
```

Push a test commit as the new tenant and confirm an EXISTING tenant's token cannot read
it (a `404`, not a `403` — see the Tenancy Isolation Contract for why the response code
matters). This is exactly what `tests/test_server.py::TestHardTenancy` proves
automatically in CI; doing it once by hand for a new production tenant is cheap
insurance.
