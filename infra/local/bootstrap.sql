-- Development credentials only. Production roles are provisioned separately.
CREATE ROLE ledgerguard_migrator LOGIN PASSWORD 'local-migrator' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE ledgerguard_app LOGIN PASSWORD 'local-development' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE ledgerguard_maintenance LOGIN PASSWORD 'local-maintenance' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
GRANT CONNECT ON DATABASE ledgerguard TO ledgerguard_migrator, ledgerguard_app, ledgerguard_maintenance;
GRANT USAGE,CREATE ON SCHEMA public TO ledgerguard_migrator;
GRANT USAGE ON SCHEMA public TO ledgerguard_app, ledgerguard_maintenance;
ALTER DEFAULT PRIVILEGES FOR ROLE ledgerguard_migrator IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO ledgerguard_app,ledgerguard_maintenance;
ALTER DEFAULT PRIVILEGES FOR ROLE ledgerguard_migrator IN SCHEMA public GRANT USAGE,SELECT ON SEQUENCES TO ledgerguard_app,ledgerguard_maintenance;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
