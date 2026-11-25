#!/bin/bash
# Local checks and explicit release publication. Compatible with macOS bash 3.2.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

usage() {
    cat <<'HELP'
Использование: ./Scripts/pipeline.sh [check|dmg|release] [--manual-checked]

  check     Переводы, версии, тесты, release-сборка .app, подпись и медиахелпер.
            Режим по умолчанию. Не запускает приложение и не публикует ничего.
  dmg       Те же проверки + локальный .dmg (сборка приложения один раз).
  release   Те же проверки + release.sh: тег, публикация в origin и ожидание CI.
            Нужен --manual-checked: ручной чеклист уже пройден.

Версию и docs/releases/<версия>.md подготовьте сами. Скрипт их не меняет.
Запуск с файлом на полке проверяет build.yml в CI. Интерфейс проверяется руками:
docs/release-checklist.md. Для подписи релиза: docs/signing.md.
HELP
}

fail() { echo "!!! $*" >&2; exit 1; }

github_repo() {
    local repo
    case "$1" in
        https://github.com/*) repo="${1#https://github.com/}" ;;
        git@github.com:*) repo="${1#git@github.com:}" ;;
        *) fail "origin должен указывать на репозиторий github.com: $1" ;;
    esac
    repo="${repo%/}"
    repo="${repo%.git}"
    [[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail "неверный адрес репозитория: $1"
    printf '%s\n' "$repo" | tr '[:upper:]' '[:lower:]'
}

MODE="${1:-check}"
case "$MODE" in
    -h|--help) [ "$#" -eq 1 ] || fail "лишние аргументы"; usage; exit 0 ;;
    check|dmg) [ "$#" -le 1 ] || fail "лишние аргументы: см. --help" ;;
    release)
        [ "$#" -eq 2 ] && [ "$2" = "--manual-checked" ] \
            || fail "сначала пройдите docs/release-checklist.md, затем release --manual-checked"
        ;;
    *) usage >&2; fail "неизвестный режим: $MODE" ;;
esac

for tool in python3 swift clang codesign; do
    command -v "$tool" >/dev/null || fail "не найден $tool; нужны Command Line Tools или Xcode"
done

echo "==> переводы и версия сайта"
# Same key detection as build.yml; run before the expensive Swift build.
python3 - <<'PY'
from pathlib import Path
import re
import sys

def fail(message):
    sys.exit(f"!!! {message}")

version_file = Path("Scripts/version").read_text(encoding="utf-8").rstrip("\r\n")
match = re.fullmatch(r"VERSION=([0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?)", version_file)
if not match:
    fail("Scripts/version: ожидается VERSION=X.Y.Z")
version = match.group(1)
site_versions = re.findall(r'"softwareVersion"\s*:\s*"([^"]+)"', Path("docs/index.html").read_text(encoding="utf-8"))
if site_versions != [version]:
    fail(f"docs/index.html: softwareVersion={site_versions}, Scripts/version={version}")

pattern = re.compile(r'\b(?:localized|Text|Button|TextField)\(\s*"((?:[^"\\]|\\.)+)"')
code = set()
for source in Path("Sources").rglob("*.swift"):
    for match in pattern.finditer(source.read_text(encoding="utf-8")):
        key = match.group(1)
        if "\\(" not in key and any(c.isalpha() for c in key):
            code.add(key.replace("\\n", "\n"))
bad = False
for language in ("en", "ru"):
    table = Path(f"Resources/{language}.lproj/Localizable.strings")
    if not table.is_file():
        fail(f"нет {table}")
    keys = {key.replace("\\n", "\n") for key in re.findall(
        r'^"((?:[^"\\]|\\.)*)"\s*=', table.read_text(encoding="utf-8"), re.M)}
    missing = code - keys
    if missing:
        bad = True
        print(f"{table}: нет ключей: {sorted(missing)}", file=sys.stderr)
if bad:
    sys.exit(1)
print(f"    версия {version}, обе локализации проверены")
PY
VERSION="$(sed -n 's/^VERSION=//p' Scripts/version | tr -d '\r')"

if [ "$MODE" = release ]; then
    echo "==> готовность к публикации"
    command -v gh >/dev/null || fail "нужен GitHub CLI: brew install gh"
    [ "$(git branch --show-current)" = main ] || fail "выпускайте из main"
    [ -z "$(git status --porcelain)" ] || fail "сначала закоммитьте изменения"
    [ -s "docs/releases/$VERSION.md" ] || fail "нужны непустые docs/releases/$VERSION.md"
    REPO="$(github_repo "$(git remote get-url origin)")"
    PUSH_URLS="$(git remote get-url --push --all origin)"
    while IFS= read -r push_url; do
        [ "$(github_repo "$push_url")" = "$REPO" ] \
            || fail "origin получает из $REPO, но отправляет в $push_url; исправьте push URL перед выпуском"
    done <<< "$PUSH_URLS"
    # gh may otherwise select a fork's parent repository. Pin every call to origin,
    # including calls inside the existing release.sh.
    export GH_REPO="$REPO"
    gh auth status >/dev/null 2>&1 || fail "войдите в GitHub CLI: gh auth login"
    git fetch --quiet origin --tags
    [ "$(git rev-parse HEAD)" = "$(git rev-parse refs/remotes/origin/main)" ] \
        || fail "локальный main должен совпадать с origin/main"
    if git show-ref --verify --quiet "refs/tags/v$VERSION"; then
        fail "тег v$VERSION уже есть — поднимите версию"
    fi
    SECRETS="$(gh secret list --repo "$REPO" --json name --jq '.[].name')"
    for name in DEVELOPER_ID_P12 DEVELOPER_ID_P12_PASSWORD NOTARY_KEY_ID NOTARY_ISSUER_ID NOTARY_KEY_P8; do
        printf '%s\n' "$SECRETS" | grep -qx "$name" || fail "в $REPO нет секрета $name; см. docs/signing.md"
    done
    CI="$(gh run list --repo "$REPO" --commit "$(git rev-parse HEAD)" --workflow build.yml --json conclusion --jq '.[0].conclusion // "none"')"
    [ "$CI" = success ] || fail "build.yml на текущем коммите: $CI; дождитесь зелёного CI"
    echo "    публикация в $REPO, тег v$VERSION"
fi

echo "==> тесты"
./Scripts/test.sh

echo "==> сборка"
if [ "$MODE" = dmg ]; then
    ./Scripts/dmg.sh
else
    ./Scripts/bundle.sh release
fi

echo "==> состав и версия приложения"
python3 - "$VERSION" <<'PY'
from pathlib import Path
import os
import plistlib
import sys

app = Path("build/Cyclop.app/Contents")
required = ("MacOS/Cyclop", "Resources/libcyclopmedia.dylib", "Info.plist",
            "Resources/en.lproj/Localizable.strings", "Resources/ru.lproj/Localizable.strings")
for name in required:
    if not (app / name).is_file():
        sys.exit(f"!!! в приложении нет {name}")
if not os.access(app / "MacOS/Cyclop", os.X_OK):
    sys.exit("!!! Cyclop не исполняемый файл")
with (app / "Info.plist").open("rb") as stream:
    version = plistlib.load(stream).get("CFBundleShortVersionString")
if version != sys.argv[1]:
    sys.exit(f"!!! версия приложения {version}, ожидается {sys.argv[1]}")
PY
codesign --verify --strict --deep "$ROOT/build/Cyclop.app"

echo "==> Now Playing helper"
./Scripts/test-helper.sh

echo "==> локальные проверки пройдены: $ROOT/build/Cyclop.app"
echo "    Вручную: docs/release-checklist.md. Проверка запуска с файлом — в build.yml на GitHub."

if [ "$MODE" = release ]; then
    ./Scripts/release.sh
    echo "==> ожидание release.yml для v$VERSION"
    RUN_ID=""
    for attempt in {1..12}; do
        RUN_ID="$(gh run list --repo "$REPO" --workflow release.yml --branch "v$VERSION" \
            --commit "$(git rev-parse HEAD)" --event push --json databaseId --jq '.[0].databaseId // empty')"
        [ -z "$RUN_ID" ] || break
        sleep 5
    done
    [ -n "$RUN_ID" ] || fail "тег уже отправлен, но запуск release.yml не найден; проверьте Actions в $REPO, повторно тег не создавайте"
    gh run watch "$RUN_ID" --repo "$REPO" --exit-status
    gh release view "v$VERSION" --repo "$REPO" --json url --jq .url
    echo "==> релиз опубликован. Скачайте образ из релиза и проверьте запуск."
fi
