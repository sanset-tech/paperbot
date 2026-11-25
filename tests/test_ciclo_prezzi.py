"""Test di scripts/ciclo_prezzi.sh contro un repository finto (nessuna rete).

Riproduce il caso del 25/11: con il ciclo anticipato, un avvio puntuale durerebbe più del timeout
del job (358 min). Il ciclo deve fermarsi da solo dopo DURATA_MAX anche se l'ora di fine è lontana.
"""
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ciclo_prezzi.sh"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git non disponibile")


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo_finto(tmp_path):
    remoto = tmp_path / "remoto.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remoto))
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(remoto), str(clone))
    _git(clone, "checkout", "-q", "-b", "main")
    (clone / "docs").mkdir()
    (clone / "docs" / "live.json").write_text('{"aggiornato_utc": "2000-01-01T00:00:00Z"}')
    _git(clone, "-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
    _git(clone, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    _git(clone, "push", "-q", "origin", "main")
    _git(clone, "branch", "-q", "--set-upstream-to=origin/main", "main")
    return remoto, clone


def test_ciclo_si_ferma_alla_durata_massima(tmp_path):
    remoto, clone = _repo_finto(tmp_path)
    env = dict(os.environ,
               PREZZI_CMD='date +%s%N > docs/live.json',
               INTERVALLO_S="1", DURATA_MAX_S="3")
    t0 = time.time()
    # 23:59 è quasi sempre lontano: senza il limite il ciclo andrebbe avanti per ore (timeout del test).
    r = subprocess.run(["bash", str(SCRIPT), "23:59"], cwd=clone, env=env,
                       capture_output=True, text=True, timeout=60)
    durata = time.time() - t0
    assert r.returncode == 0, r.stdout + r.stderr
    assert durata < 30
    giri = r.stdout.count("== giro")
    assert 1 <= giri <= 5, r.stdout
    log = subprocess.run(["git", "--git-dir", str(remoto), "log", "--oneline", "main"],
                         capture_output=True, text=True, check=True).stdout
    assert log.count("Prezzi e candele") == giri  # ogni giro pubblicato


def test_singolo_salta_se_prezzi_freschi(tmp_path):
    _, clone = _repo_finto(tmp_path)
    ora = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (clone / "docs" / "live.json").write_text('{"aggiornato_utc": "%s"}' % ora)
    _git(clone, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "fresco")
    _git(clone, "push", "-q", "origin", "main")
    env = dict(os.environ, PREZZI_CMD="echo NON-DEVE-GIRARE")
    r = subprocess.run(["bash", str(SCRIPT), "singolo"], cwd=clone, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    assert "salto" in r.stdout
    assert "NON-DEVE-GIRARE" not in r.stdout
