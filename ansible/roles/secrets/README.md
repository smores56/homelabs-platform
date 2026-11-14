# secrets

Installs operator-provided secret files without Nix, SOPS-on-host, or a
secret value in git.

The operator decrypts the SOPS bundle **outside the repository** into a
temporary directory, then runs:

```sh
ansible-playbook playbooks/site.yml --tags secrets \
  -e secrets_source_dir=/run/operator-secrets \
  -e '{"secrets_files":["event-service.env","write-broker.env","restic-password"]}'
```

The role refuses an empty or relative source directory, refuses a path inside
`.git`, creates `/etc/homelab/secrets` at `0700`, and copies each file at
`0600` root:root with `no_log`. It never decrypts or prints a value.

After the run, wipe the temporary directory. The decrypted files are the only
copy on the host; keep the SOPS ciphertext in the repository as the source of
truth.
