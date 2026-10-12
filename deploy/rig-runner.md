# Rig runner setup (one-time, on the board)

Turns the Orange Pi into the test rig for agent PRs: a GitHub Actions
self-hosted runner that runs `.github/workflows/rig-test.yml`, plus ssh
access for `scripts/remote_rig_test.sh`. Run as `hamish` on the board.

## 1. Passwordless afl-camd stop/start

`rig_test.sh` stops the installed daemon to free the cameras and restarts it
after. Nothing else gets sudo.

```bash
echo 'hamish ALL=(root) NOPASSWD: /usr/bin/systemctl stop afl-camd, /usr/bin/systemctl start afl-camd' \
  | sudo tee /etc/sudoers.d/afl-cam-rig
sudo chmod 440 /etc/sudoers.d/afl-cam-rig && sudo visudo -c
```

## 2. GitHub Actions runner

Keep the repo **private**: a self-hosted runner on the home network must
never run code from strangers' PRs (the workflow also refuses fork PRs).

1. GitHub → repo **Settings → Actions → Runners → New self-hosted runner**,
   pick **Linux / ARM64**, and run the download + `config.sh` lines it
   shows (they carry the current version and a one-hour token). When
   `config.sh` asks for labels, enter `afl-cam-rig`.
2. Install it as a service running as `hamish` (needs the video group,
   GPU and cameras like the daemon):
   ```bash
   cd ~/actions-runner
   sudo ./svc.sh install hamish && sudo ./svc.sh start
   ```
3. Check: the runner shows **Idle** on that settings page, then
   **Actions → rig-test → Run workflow** on `main` goes green.

Jobs run one at a time, so parallel PRs queue for the cameras.

## 3. ssh for local sessions

`remote_rig_test.sh` uses `ssh hamish@orangepi5-max` (override with `AFL_CAM_PI`).
With Tailscale on both machines, either enable Tailscale SSH on the board
(`sudo tailscale set --ssh`) or put your dev machine's public key in
`~/.ssh/authorized_keys`. On Windows run it from Git Bash or WSL.

## 4. Remote desktop for the GTK UI

For eyeballing the on-device UI from your PC:
- GNOME on Wayland: **Settings → Sharing → Remote Desktop** (RDP), then
  connect with Windows' Remote Desktop Connection to the board's Tailscale
  name.
- X11 session: `sudo apt install x11vnc` and `x11vnc -display :0 -localhost`
  over an ssh tunnel.

Disconnect before performance tests; the remote session costs CPU/GPU.

## 5. Issue labels

From any machine with `gh` authenticated: `bash .github/labels.sh`.
