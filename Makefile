# -------- Config (override on command line or via env) --------
PI_HOST ?= pi@pi-tl
PI_DIR  ?= /home/pi/timelapse
SERVICE ?= timelapse.service
BRANCH  ?= main

# Commit message (override: make deploy MSG="your message")
MSG ?= quick deploy

# -------- Helpers --------
define yellow
    @printf "\033[33m%s\033[0m\n" "$(1)"
endef

# -------- Git steps (local) --------
.PHONY: commit push
commit:
	git add -A
	git commit -m "$(MSG)" || true

push:
	$(call yellow,"[local] Pushing to origin $(BRANCH)…")
	git push origin $(BRANCH)

# -------- Remote steps (on Pi) --------
.PHONY: pi-pull pi-restart pi-logs pi-status
pi-pull:
	$(call yellow,"[pi] Pulling latest on $(PI_DIR)…")
	ssh $(PI_HOST) 'set -e; cd $(PI_DIR) && git fetch --all && git checkout $(BRANCH) && git pull --ff-only'

pi-restart:
	$(call yellow,"[pi] Restarting $(SERVICE)…")
	ssh $(PI_HOST) 'sudo systemctl restart $(SERVICE)'

pi-status:
	$(call yellow,"[pi] Status of $(SERVICE)…")
	ssh $(PI_HOST) 'systemctl --no-pager --full status $(SERVICE)'

pi-logs:
	$(call yellow,"[pi] Tailing logs (ctrl+c to stop)…")
	ssh $(PI_HOST) 'journalctl -u $(SERVICE) -f -n 50 --no-pager'

# -------- One-shot targets --------
.PHONY: deploy logs status deploy-force

deploy: commit push pi-pull pi-restart
	$(call yellow,✅ Deploy complete.)

# Force: overwrite any local Pi changes with what's on origin/$(BRANCH)
deploy-force: commit push pi-pull-hard pi-restart
	$(call yellow,✅ Force deploy complete (Pi reset to origin/$(BRANCH)).)

.PHONY: pi-pull-hard
pi-pull-hard:
	$(call yellow,[pi] Forcing repo to origin/$(BRANCH) on $(PI_DIR)…)
	ssh $(PI_HOST) 'set -e; cd $(PI_DIR) && \
		git fetch --all && \
		echo "[pi] backing up uncommitted changes (if any)..." && \
		( git diff > .deploy_backup_$$HOSTNAME_$$(date +%Y%m%d-%H%M%S).patch || true ) && \
		git reset --hard origin/$(BRANCH)'

logs: pi-logs
status: pi-status