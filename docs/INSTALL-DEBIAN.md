<!--
SPDX-License-Identifier: GPL-3.0-or-later
SPDX-FileCopyrightText: 2026 Claire Ivanenka <claire@gnu-ai.org>

This file is part of the GNU AI stack and is free software: you can
redistribute it and/or modify it under the terms of the GNU
General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version.
-->

# Installation sur hôte Debian Linux + VM GNU/Hurd amd64

Procédure validée (Debian 13 « trixie », QEMU 10 en TCG ou KVM,
image `debian-hurd-amd64-20260314.img`) : installer le driver,
télécharger l'image officielle, recompiler **les cinq translators**
dans la VM, puis monter et tester la pile GNU AI dans l'invité.

Variante Android/Termux : voir [TERMUX.md](TERMUX.md) (même VM,
QEMU en TCG pur).

## 1. Prérequis (hôte Debian)

```console
sudo apt update
sudo apt install qemu-system-x86 e2fsprogs python3
# utile seulement pour l'injection hors-ligne des sources (section 5) :
sudo apt install autoconf automake libtool git xz-utils
```

- `qemu-system-x86` : la VM. KVM est utilisé automatiquement si
  `/dev/kvm` existe, sinon repli sur TCG (`-accel tcg`) — aucun réglage.
- `e2fsprogs` (`e2fsck`, `debugfs`, dans `/usr/sbin` — le driver les
  cherche aussi là) : préparation hors-ligne de la copie CI de l'image.
- `python3` (3.8+) : le driver `hurd_vm.py`.
- ~10 Gio libres : image 4 Gio + copie CI 4 Gio + logs.

## 2. Le dépôt et l'image

```console
git clone https://github.com/GNU-AI-SOFTWARE/mistral-vm-debian-hurd
cd mistral-vm-debian-hurd
wget https://cdimage.debian.org/cdimage/ports/latest/hurd-amd64/debian-hurd-amd64-20260314.img.tar.xz
tar xJf debian-hurd-amd64-20260314.img.tar.xz
```

## 3. Recompiler les cinq translators dans la VM

```console
python3 hurd_vm.py debian-hurd-amd64-20260314.img guest-all-translators.sh --ram 2G
```

Le driver prépare une copie CI de l'image (`debian-hurd-ci.img`, en
cache entre les runs), patch `grub.cfg` pour la console série
(`console=com0`), démarre QEMU, se logue en `root` sur la ligne
série et exécute `guest-all-translators.sh` dans l'invité :
récupération des sources (clone GitHub, ou tarball injecté — section
5), puis `./autogen.sh && ./configure && make && make check` pour
`neuron-translator`, `orchestrator-translator`, `inference-translator`,
`httpfs-translator`, `data-base-translator`.

Résultat attendu (vu dans `serial.log`, code de sortie 0) :

```
DNS-OK  HTTP-OK  APT-OK
BUILD-neuron-translator-OK       CHECK-neuron-translator-OK
BUILD-orchestrator-translator-OK CHECK-orchestrator-translator-OK
BUILD-inference-translator-OK    CHECK-inference-translator-OK
BUILD-httpfs-translator-OK       CHECK-httpfs-translator-OK
BUILD-data-base-translator-OK    CHECK-data-base-translator-OK
ALL-TRANSLATORS-BUILT
GUESTRC=0
```

Tests constatés : 6/6, 4/4 (+1 SKIP `test_pg.sh` sans serveur PG),
6/6, 8/8, 3/3 (+1 SKIP). Avec PostgreSQL installé dans l'invité,
`test_pg.sh` se lance en `postgres` (il crée son propre cluster
jetable ; `initdb` refuse de tourner en root) — voir section 6.

Options utiles : `--ram 2G` (défaut 1G), `--fresh` (reconstruire la
copie CI), `--keep` (VM laissée vivante, `ssh -p 2222 root@127.0.0.1`).
Timeouts surchargeables par environnement (hôtes lents) :
`HURD_VM_TIMEOUT_LOGIN` (défaut 900 s), `HURD_VM_TIMEOUT_SCRIPT`
(défaut 3600 s).

## 4. Monter et tester la pile DANS la VM

Se connecter à la VM en cours (`--keep`) via `ssh -p 2222
root@127.0.0.1`, ou relancer un script guest. Binaires buildés :
`/root/src/<projet>/src/<binaire>`. **Noms réels** :

| Projet | Binaire |
|---|---|
| neuron-translator | `sigmoid-neuron-translator` |
| orchestrator-translator | `orchestrator-translator` (trivfs, fichier unique) |
| inference-translator | `inference-translator` (translator) + `inference` (client) |
| httpfs-translator | `httpfs` (+ `htmlfs jsonfs csvfs tsvfs`) |
| data-base-translator | `db-translator` (translator) + `db-translator-repl` (stdio) |

### 4.1 PostgreSQL (pour `/db`) — en tant que `postgres`

```console
runuser -u postgres -- sh -c '/usr/lib/postgresql/19/bin/initdb -D /tmp/pg -U gnuai --auth=trust'
runuser -u postgres -- sh -c '/usr/lib/postgresql/19/bin/pg_ctl -D /tmp/pg -l /tmp/pg.log -o "-p 5499 -c listen_addresses=127.0.0.1 -c unix_socket_directories=/tmp" -w start'
runuser -u postgres -- sh -c '/usr/lib/postgresql/19/bin/createdb -h 127.0.0.1 -p 5499 -U gnuai gnuai'
```

Le schéma (`schema.sql`) est appliqué idempotent au premier branche
par `db-translator` lui-même (`CREATE TABLE IF NOT EXISTS`).

### 4.2 Monter les translators

**Toujours `mkdir` le nœud avant `settrans -a`** (sinon « No such file
or directory » ; ne jamais réutiliser un nœud créé par une redirection
`>` échouée) :

```console
settrans -g /db 2>/dev/null; settrans -g /llm1 2>/dev/null; settrans -g /orchestrate 2>/dev/null
rm -rf /db /llm1 /orchestrate /inference
mkdir -p /db /llm1 /orchestrate /inference

settrans -a /db /root/src/data-base-translator/src/db-translator --conninfo 'host=127.0.0.1 port=5499 dbname=gnuai user=gnuai'
settrans -a /llm1 /root/src/neuron-translator/src/sigmoid-neuron-translator
settrans -a /orchestrate /root/src/orchestrator-translator/src/orchestrator-translator --neuron /root/src/neuron-translator/src/sigmoid-neuron-translator
settrans -a /inference /root/src/inference-translator/src/inference-translator
showtrans /db /llm1 /orchestrate /inference
```

- `db-translator` **exige `--conninfo`** au montage.
- `orchestrator-translator` monte lui-même les instances `/llm<N>` au
  `run` avec le binaire donné par `--neuron` (défaut
  `/hurd/sigmoid-neuron-translator`, à écraser) ; stockage par défaut
  `db:/db` (`--storage mem` pour un test sans PG).
- `/orchestrate` est un **fichier trivfs unique** : on écrit la
  commande JSON et on relit la réponse sur `/orchestrate` — il n'y a
  pas de sous-nœuds `/orchestrate/status`.

### 4.3 Piloter le neurone (votre LLM)

```console
echo 'topology 2,4,1' > /llm1
echo 'load /chemin/reel/modele.nn' > /llm1     # poids entraînés, format .nn
echo 'input 0.5,0.3' > /llm1
cat /llm1
echo 'save /tmp/copie.nn' > /llm1
```

Sans `load`, les poids sont un bruit pseudo-aléatoire **déterministe**
(placeholder) : la mécanique se teste (topology → input → output), les
nombres sont du bruit tant qu'aucun `.nn` entraîné n'est chargé.
Roundtrip de vérification : `save` → `reset` → `load` → même `input`
doit redonner exactement la même sortie.

### 4.4 Orchestrateur

```console
echo '{"run": {"instances": 2, "topologies": ["2,4,1"], "input": [0.5, 0.3], "aggregate": "mean"}}' > /orchestrate
cat /orchestrate
echo '{"command": "status"}' > /orchestrate
cat /orchestrate
echo '{"command": "result"}' > /orchestrate
cat /orchestrate
```

### 4.5 Client interactif `inference`

Le client a besoin d'un **TTY** (console série OK ; `</dev/null`
refusé) et trouve `httpfs` via `$HTTPFS_BIN` :

```console
cd /root/src/inference-translator
export HTTPFS_BIN=/root/src/httpfs-translator/src/httpfs
./src/inference --mount /inference        # --db /db et --orchestrate /orchestrate par défaut
```

Dans le client (`>`) : prompt texte + `/submit`, `/watch`, `/reply …`,
`/sigmoid`, `/db status|list|search|count`, `/hf list|sync hf://…`,
`/entrainement URL|hf://… [dir]`, `/fetch URL`, `/quit`. Les commandes
`/…` ne marchent **que** dans le client, pas dans bash.

Buckets Hugging Face du projet (compte `clairemariecoralie`) :
`hf://buckets/clairemariecoralie/DeepSeek-V4.1-Flash-Nemotron-Competitive-Coding-Distill-bucket`
(dataset d'entraînement, 790 Mo), `…/multimodal-knowledge-agent-bucket`,
`…/cyber-security-bucket` (95 Gio).

## 5. Mode hors-ligne (sources injectées)

```console
tar czf all-translators-src.tar.gz -C /chemin/vers/les-sources \
    neuron-translator orchestrator-translator inference-translator \
    httpfs-translator data-base-translator
./inject-src.sh ~/all-translators-src.tar.gz    # après un premier run (copie CI créée)
python3 hurd_vm.py <image> guest-all-translators.sh
```

`guest-all-translators.sh` préfère alors `/root/all-translators-src.tar.gz`
au clone GitHub.

## 6. Pièges constatés sur Hurd (à connaître)

- `initdb`/les tests PG refusent de tourner en **root** : utiliser
  `runuser -u postgres -- sh -c '…'`.
- `getcwd()` sur Hurd exige la **lecture** sur chaque répertoire parent :
  pour lancer `make check` en `postgres` depuis `/root/src/…`, faire
  `chmod 755 /root /root/src` (ou copier les arbres côte à côte dans
  `/tmp` — orchestrator attend le voisin
  `../../data-base-translator/src/db-translator(-repl)`).
- Locale : préférer `export LANG=C.UTF-8 LC_ALL=C.UTF-8` (`en_US.UTF-8`
  n'est pas générée dans l'image → warnings `setlocale`).
- Prompt root en couleur (dans `/root/.bashrc`) :

  ```bash
  PS1='\[\033[01;31m\]\u@\h\[\033[00m\]:\[\033[01;34m\]\w\[\033[00m\]\[\033[01;31m\]\$ \[\033[00m\]'
  ```

- Réseau de l'invité : slirp (10.0.2.15, passerelle 10.0.2.2, DNS
  10.0.2.3) ; le DNS UDP de slirp est souvent cassé sur Hurd, les
  scripts guest épinglent `deb.debian.org`, `github.com`,
  `huggingface.co`, `datasets-server.huggingface.co` dans `/etc/hosts`.
  Diagnostic : `guest-netfix.sh`. Ne jamais faire `settrans -fg` sur
  `/servers/socket/2` (tue netdde et `/dev/eth0`).

## 7. Sécurité

Par défaut tout reste sur `127.0.0.1` de l'hôte (console série en
socket TCP éphémère, forward SSH `127.0.0.1:2222→:22`). Ne pas définir
`HURD_VM_SSH_BIND` sur une adresse publique ; sur un LAN, protéger
l'accès root avec `--ssh-key` plutôt qu'un mot de passe.

Claire Ivanenka — claire@gnu-ai.org
GNU AI — https://gnu-ai.org
