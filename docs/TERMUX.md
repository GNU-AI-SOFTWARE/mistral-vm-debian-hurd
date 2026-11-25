<!--
SPDX-License-Identifier: GPL-3.0-or-later
SPDX-FileCopyrightText: 2026 Claire Ivanenka <claire@gnu-ai.org>

This file is part of the GNU AI stack and is free software: you can
redistribute it and/or modify it under the terms of the GNU
General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version.
-->

# Faire tourner la VM Hurd sous Termux (Android) et y recompiler la pile

`mistral-vm-debian-hurd` tourne aussi bien sur un téléphone Android
équipé de **Termux** : QEMU existe en paquet Termux, et comme Android
n'expose pas `/dev/kvm`, le driver `hurd_vm.py` bascule **tout seul**
sur l'émulation TCG (`-accel tcg`) — aucun réglage à faire.

La procédure ci-dessous recompile **les cinq translators** de la pile
GNU AI sur le vrai GNU/Hurd, dans la VM, depuis le téléphone :

```
Android (Termux)  ── QEMU (TCG, sans KVM)  ──▶  Debian GNU/Hurd  ──▶  guest-all-translators.sh
                                                              └─▶  build + make check des 5 translators
```

## 1. Préparation de Termux

```console
$ pkg update
$ pkg install qemu-system-x86-64-headless python e2fsprogs xz-utils tar wget git
$ termux-setup-storage        # autorise l'accès au stockage partagé
$ qemu-system-x86_64 --version   # vérifie le binaire (installé dans $PREFIX/bin)
```

- `qemu-system-x86-64-headless` : la machine virtuelle. C'est le nom
  actuel du paquet dans le dépôt principal Termux (`qemu-system-x86-64`
  n'existe plus comme paquet installable ; la variante graphique vit
  dans le dépôt x11).  Le paquet headless installe bien le binaire
  **`qemu-system-x86_64`** dans `$PREFIX/bin` — c'est exactement ce que
  `hurd_vm.py` appelle — compilé sans SDL/GTK, avec slirp (réseau
  user-mode) inclus : parfait pour `-display none`.  Émulation pure
  (TCG).  **Attention architecture** : sur ARM 32 bits
  (`uname -m` renvoie `armv7l`/`armv8l`) le paquet est exclu du dépôt
  officiel ; il faut alors activer TUR :
  `pkg install tur-repo && pkg update && pkg install qemu-system-x86-64-headless` ;
- `python` : le driver `hurd_vm.py` ;
- `e2fsprogs` : `e2fsck`/`debugfs`, utilisés hors-ligne pour préparer la
  copie CI de l'image (le driver les cherche aussi dans `/sbin`) ;
- `xz-utils`, `tar`, `wget` : télécharger et déballer l'image ;
- `git` : cloner les sources des translators *dans* la VM si besoin.

## 2. L'image et le dépôt

```console
$ cd ~
$ git clone https://github.com/GNU-AI-SOFTWARE/mistral-vm-debian-hurd
$ cd mistral-vm-debian-hurd
$ wget https://cdimage.debian.org/cdimage/ports/latest/hurd-amd64/debian-hurd-amd64-20260314.img.tar.xz
$ tar xJf debian-hurd-amd64-20260314.img.tar.xz     # image de 4 Gio
```

## 3. Recompiler tous les translators dans la VM

```console
$ python3 hurd_vm.py debian-hurd-amd64-20260314.img guest-all-translators.sh --ram 1G
```

Le driver prépare une copie CI de l'image (`debian-hurd-ci.img`, mise
en cache entre les exécutions), démarre QEMU, se connecte en `root` sur
la ligne série, puis exécute `guest-all-translators.sh` dans l'invité :
récupération des sources des cinq translators, `./autogen.sh`,
`./configure`, `make`, `make check` pour chacun. Les jalons
`BUILD-<projet>-OK`, `CHECK-<projet>-OK` et `ALL-TRANSLATORS-BUILT`
apparaissent sur la console série (aussi sauvés dans `serial.log`), et
le code de sortie du script devient celui du driver.

### Sur un téléphone : mémoire et timeouts

- Gardez `--ram` à **1G** (défaut) voire moins : la VM partage la RAM
  du téléphone avec Android. Ne dépassez pas la moitié de la RAM
  disponible.
- L'émulation TCG est lente : le boot peut prendre 10 à 30 minutes.
  Les timeouts du driver se règlent par variables d'environnement :

```console
$ HURD_VM_TIMEOUT_LOGIN=3600 HURD_VM_TIMEOUT_SCRIPT=14400 \
    python3 hurd_vm.py debian-hurd-amd64-20260314.img guest-all-translators.sh --ram 1G
```

(`HURD_VM_TIMEOUT_LOGIN` : attente du prompt de login, défaut 900 s ;
`HURD_VM_TIMEOUT_SCRIPT` : durée maximale du script invité, défaut
3600 s.)

### Un seul translator

Éditez la liste `PROJECTS` en tête de `guest-all-translators.sh`, ou
utilisez les scripts dédiés existants : `guest-httpfs.sh`
(httpfs-translator) ; pour inference-translator, voir le répertoire
`hurd-vm/` de ce dépôt (`guest-inference.sh`).

### Mode hors-ligne (sources injectées)

Si le réseau de l'invité est inutilisable, injectez les sources depuis
Termux avant le run (pas besoin de démarrer la VM pour ça, mais la
copie CI doit exister — un premier `python3 hurd_vm.py <image>` la
crée) :

```console
$ tar czf all-translators-src.tar.gz -C /chemin/vers/les-sources \
      neuron-translator orchestrator-translator inference-translator \
      httpfs-translator data-base-translator
$ ./inject-src.sh ~/all-translators-src.tar.gz
$ python3 hurd_vm.py debian-hurd-amd64-20260314.img guest-all-translators.sh
```

`guest-all-translators.sh` préfère alors `/root/all-translators-src.tar.gz`
au clone GitHub.

## 4. Sécurité réseau

Par défaut, tout reste sur **127.0.0.1** à l'intérieur du bac à sable
Termux : la console série (socket TCP éphémère) et le forward SSH
`127.0.0.1:2222 -> :22`. Ne définissez **pas** `HURD_VM_SSH_BIND` sur une
adresse publique, et si vous l'activez sur votre LAN, protégez l'accès
root avec `--ssh-key` (clé) plutôt qu'un mot de passe.

## 5. Récupérer les binaires compilés

Pendant que la VM tourne (`--keep`), ou via la console série :

```console
# hôte (Termux) :
$ python3 hurd_vm.py debian-hurd-amd64-20260314.img guest-all-translators.sh --keep --ram 1G
$ scp -P 2222 root@127.0.0.1:/root/neuron-translator/src/neuron-translator ~/bin-hurd/
```

Les binaires produits sont des **ELF GNU/Hurd x86-64** : ils tournent
dans la VM (et dans toute Debian GNU/Hurd amd64), pas nativement sous
Android — d'où la VM. Pour un binaire *natif* Android/Termux
(`aarch64-linux-android`), il faut une chaîne de cross-compilation NDK,
hors du périmètre de cette VM.

## 6. Dépannage

- **`virtual machine already running`** : un QEMU précédent tient la
  copie CI ; tuez-le (`pkill qemu-system-x86`) ou passez `--fresh`.
- **Boot qui fige après `start pci-arbiter: `** : voir *Why we do not
  build a custom GRUB* dans le README — n'utilisez que l'image
  officielle et son propre GRUB patché par le driver.
- **Pas de réseau dans l'invité** (`DNS-KO`/`HTTP-KO`) : `guest-netfix.sh`
  est le script de diagnostic ; les sources injectées (section 3)
  permettent de builder hors-ligne malgré tout.
- **Timeout au login sur téléphone lent** : augmentez
  `HURD_VM_TIMEOUT_LOGIN` (section 3).

Claire Ivanenka — claire@gnu-ai.org
GNU AI — https://gnu-ai.org
