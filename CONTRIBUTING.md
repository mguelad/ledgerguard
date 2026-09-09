# Engineering changes

Describe the concrete failure, intended behavior and evidence supporting the change. Use synthetic fixtures; never commit merchant exports or provider payloads. Keep financial decisions in the framework-free engine. Pass time explicitly. Money uses checked integers with an explicit exponent.

Every tenant model requires organization scope, FORCE RLS, composite tenant foreign keys and isolation tests. Runtime must remain a non-owner without BYPASSRLS. Run migrations as the separate migrator. Never weaken immutable-table permissions to simplify a feature.

Version schemas and rule behavior deliberately. Preserve golden fixtures across changes. Review provider permissions and outgoing calls; financial write operations are outside v1. Run `make check` and the disposable PostgreSQL suite. Change migrations using expand/contract steps so an application rollback remains possible.

Pull requests explain the problem, changed behavior, verification and operational consequences. Required checks cover Python, supported plugin combinations, infrastructure and container security. Dependency updates require a locked diff and relevant regressions. An unexecuted external gate must remain visibly open.
