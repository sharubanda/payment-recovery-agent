"""Per-merchant policy overrides: JSON files an ops team edits, validated once at startup.

    merchants/<merchant_id>.json      overrides for one merchant (PaymentAttempt.merchant_id)
    merchants/_default.json           overrides every merchant inherits unless its own file says otherwise
    merchants/_example.json           documentation only: any other file starting with "_" is skipped

Schema (every field optional; unknown keys are rejected, so a typo cannot silently do nothing):

    {
      "classes": {
        "INSUFFICIENT_FUNDS": {"delay_seconds": 86400, "max_attempts": 3, "nudge": false, "action": "human_queue"}
      },
      "nudge_language": "hi",             en | hi | hinglish
      "max_contacts_per_week": 2,         0..50 link/nudge sends per customer phone or email per rolling week
      "notify_customer": false,           Razorpay notify.sms/email on the link (never turned on by a file
                                          alone: the global RAZORPAY_NOTIFY_CUSTOMER must be on as well)
      "disabled_actions": ["token_retry"],  token_retry | recovery_link | nudge_change_method
      "human_queue_all": false            kill switch: every decision for this merchant goes to a person
    }

What a file may NOT do, enforced in code (app/policy.py) and pinned by tests/test_merchants.py:
RISK_BLOCKED and UNKNOWN stay human_queue whatever the file says; a class override may only set
"action" to "human_queue" (a merchant can make the agent more conservative, never less: no file
turns a link into a token charge); delay_seconds must be >= 0; max_attempts must be in 0..5.

Files are read once per process and cached; reload() re-reads them. A malformed file raises
MerchantConfigError naming the file and the field, and the CLI exits 1 rather than falling back
to the defaults: an override that quietly did not apply is the worst outcome in a money path.

The pipeline is expected to call `policy.decide(..., overrides=merchants.for_attempt(attempt))`
(app/pipeline.py) and the executor to read `nudge_language`, `max_contacts_per_week` and
`notify_customer` through `for_attempt` (app/executor.py); see docs/merchants.md.
"""
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import config
from .taxonomy import Action, FailureClass

DEFAULT_FILE = "_default.json"
MAX_ATTEMPTS_CEILING = 5
CONTACT_CAP_CEILING = 50
LANGUAGES = ("en", "hi", "hinglish")
# the human queue is the conservative end of every override; the terminal actions cannot be disabled
DISABLEABLE_ACTIONS = (Action.TOKEN_RETRY.value, Action.RECOVERY_LINK.value, Action.NUDGE_CHANGE_METHOD.value)
# classes whose table action is human_queue: an override may not move them anywhere else
PINNED_TO_HUMAN = (FailureClass.RISK_BLOCKED.value, FailureClass.UNKNOWN.value)
# write_override / rollback_override keep their trail under this key; parse_file ignores it
HISTORY_KEY = "_history"
ALL_MERCHANTS = "all"  # merchant_id meaning "every merchant": the write goes to _default.json


class MerchantConfigError(ValueError):
    """A merchant file that must not be loaded. The message names the file and the field."""


class ClassOverride(BaseModel):
    """Overrides for one failure class. `action` may only be "human_queue": the table's own action
    is the default, so naming it is a no-op and naming any other is refused."""
    model_config = ConfigDict(extra="forbid")

    delay_seconds: int | None = Field(default=None, ge=0)
    max_attempts: int | None = Field(default=None, ge=0, le=MAX_ATTEMPTS_CEILING)
    nudge: bool | None = None
    action: str | None = None

    @field_validator("action")
    @classmethod
    def _only_more_conservative(cls, value):
        if value is None:
            return None
        if value != Action.HUMAN_QUEUE.value:
            raise ValueError(f"action may only be {Action.HUMAN_QUEUE.value!r} (or omitted to keep the table's action); "
                             f"a merchant file can make the agent more conservative, never less; got {value!r}")
        return value

    def fields_set(self) -> list[str]:
        return [name for name in ("delay_seconds", "max_attempts", "nudge", "action") if getattr(self, name) is not None]


class MerchantPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classes: dict[str, ClassOverride] = Field(default_factory=dict)
    nudge_language: Literal["en", "hi", "hinglish"] | None = None
    max_contacts_per_week: int | None = Field(default=None, ge=0, le=CONTACT_CAP_CEILING)
    notify_customer: bool | None = None
    disabled_actions: list[str] = Field(default_factory=list)
    human_queue_all: bool = False

    @field_validator("classes")
    @classmethod
    def _known_classes(cls, value: dict[str, ClassOverride]):
        known = {c.value for c in FailureClass}
        for name, override in value.items():
            if name not in known:
                raise ValueError(f"unknown failure class {name!r}; one of {', '.join(sorted(known))}")
            if name in PINNED_TO_HUMAN and override.fields_set() not in ([], ["action"]):
                raise ValueError(f"{name} is always human_queue (delay, attempts and nudge cannot apply); "
                                 f"only \"action\": \"human_queue\" is accepted for it")
        return value

    @field_validator("disabled_actions")
    @classmethod
    def _disableable(cls, value: list[str]):
        for name in value:
            if name not in DISABLEABLE_ACTIONS:
                raise ValueError(f"cannot disable {name!r}; one of {', '.join(DISABLEABLE_ACTIONS)}")
        return sorted(set(value))

    def is_empty(self) -> bool:
        return (not self.classes and self.nudge_language is None and self.max_contacts_per_week is None
                and self.notify_customer is None and not self.disabled_actions and not self.human_queue_all)

    def merged_over(self, base: "MerchantPolicy | None") -> "MerchantPolicy":
        """This policy layered over `base` (the _default.json): a field set here wins, a class
        override merges field by field, disabled actions are the union, human_queue_all is OR."""
        if base is None:
            return self
        classes = {k: v.model_copy() for k, v in base.classes.items()}
        for name, mine in self.classes.items():
            merged = classes.get(name, ClassOverride()).model_dump()
            merged.update({k: v for k, v in mine.model_dump().items() if v is not None})
            classes[name] = ClassOverride(**merged)
        return MerchantPolicy(
            classes=classes,
            nudge_language=self.nudge_language if self.nudge_language is not None else base.nudge_language,
            max_contacts_per_week=(self.max_contacts_per_week if self.max_contacts_per_week is not None
                                   else base.max_contacts_per_week),
            notify_customer=self.notify_customer if self.notify_customer is not None else base.notify_customer,
            disabled_actions=sorted(set(self.disabled_actions) | set(base.disabled_actions)),
            human_queue_all=self.human_queue_all or base.human_queue_all,
        )

    def describe(self) -> str:
        """One line for the header and docs."""
        parts = []
        if self.human_queue_all:
            parts.append("human_queue_all")
        for name, o in sorted(self.classes.items()):
            parts.append(f"{name}[{', '.join(f'{f}={getattr(o, f)}' for f in o.fields_set())}]")
        if self.disabled_actions:
            parts.append(f"disabled={','.join(self.disabled_actions)}")
        if self.nudge_language:
            parts.append(f"language={self.nudge_language}")
        if self.max_contacts_per_week is not None:
            parts.append(f"contacts/week={self.max_contacts_per_week}")
        if self.notify_customer is not None:
            parts.append(f"notify={'on' if self.notify_customer else 'off'}")
        return "; ".join(parts) or "(no overrides)"


# ---- loading -----------------------------------------------------------------------------------

def merchants_dir() -> Path:
    """config.MERCHANTS_DIR if app/config.py declares it, else the MERCHANTS_DIR env var, else ./merchants."""
    value = getattr(config, "MERCHANTS_DIR", None) or os.getenv("MERCHANTS_DIR", "").strip() or "merchants"
    path = Path(value)
    return path if path.is_absolute() else config.BASE_DIR / path


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        msg = err.get("msg", "invalid")
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        lines.append(f"field {loc}: {msg}")
    return f"{path}: " + "; ".join(lines)


def parse_file(path: Path) -> MerchantPolicy:
    """One file -> MerchantPolicy, or MerchantConfigError naming the file and the field."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MerchantConfigError(f"{path}: cannot read: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise MerchantConfigError(f"{path}: not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise MerchantConfigError(f"{path}: field (root): must be a JSON object")
    data = {k: v for k, v in data.items() if k != HISTORY_KEY}  # the write/rollback trail, not policy
    try:
        return MerchantPolicy.model_validate(data)
    except ValidationError as exc:
        raise MerchantConfigError(_format_validation_error(path, exc)) from exc


class Registry:
    """Every override file in one directory, parsed and validated. `get` layers a merchant's file
    over _default.json; a merchant with no file gets the default alone (or None when there is none)."""

    def __init__(self, directory: Path, default: MerchantPolicy | None, merchants: dict[str, MerchantPolicy],
                 skipped: list[str]):
        self.directory = directory
        self.default = default
        self.merchants = merchants
        self.skipped = skipped
        self._merged: dict[str, MerchantPolicy | None] = {}

    @classmethod
    def load(cls, directory: Path) -> "Registry":
        default, merchants, skipped = None, {}, []
        if directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                if path.name == DEFAULT_FILE:
                    default = parse_file(path)
                elif path.name.startswith("_"):
                    skipped.append(path.name)  # documentation and templates, never loaded
                else:
                    merchants[path.stem] = parse_file(path)
        return cls(directory, default, merchants, skipped)

    def get(self, merchant_id: str | None) -> MerchantPolicy | None:
        key = merchant_id or ""
        if key not in self._merged:
            own = self.merchants.get(key)
            self._merged[key] = own.merged_over(self.default) if own is not None else self.default
        return self._merged[key]

    @property
    def count(self) -> int:
        return len(self.merchants) + (1 if self.default is not None else 0)

    def describe(self) -> str:
        """The demo header line: how many files, from where, and the ones that were skipped."""
        try:
            where = self.directory.relative_to(config.BASE_DIR)
        except ValueError:
            where = self.directory
        if not self.directory.is_dir():
            return f"0 merchant override files ({where} does not exist; table defaults for every merchant)"
        names = sorted(self.merchants)
        if self.default is not None:
            names.insert(0, DEFAULT_FILE[:-5])
        listed = f" ({', '.join(names)})" if names else ""
        skipped = f"; skipped {', '.join(self.skipped)} (leading underscore: documentation only)" if self.skipped else ""
        return f"{self.count} merchant override file(s) from {where}/{listed}{skipped}"


_registry: Registry | None = None


def load(directory: Path | str | None = None) -> Registry:
    """The cached registry, reading the files on the first call. Raises MerchantConfigError."""
    global _registry
    if _registry is None or directory is not None:
        _registry = Registry.load(Path(directory) if directory is not None else merchants_dir())
    return _registry


def reload(directory: Path | str | None = None) -> Registry:
    """Re-read the files from the same directory (or a new one); an ops edit takes effect at the
    next reload / process start, never mid-run."""
    global _registry
    where = Path(directory) if directory is not None else (_registry.directory if _registry is not None else None)
    _registry = None
    return load(where)


def get(merchant_id: str | None) -> MerchantPolicy | None:
    return load().get(merchant_id)


def for_attempt(attempt) -> MerchantPolicy | None:
    """The overrides for an attempt's merchant, or None when the table applies unchanged.
    The pipeline passes this to policy.decide(..., overrides=...)."""
    return get(getattr(attempt, "merchant_id", None))


def loaded_count() -> int:
    return load().count


def header_line() -> str:
    return load().describe()


def effective_table(merchant_id: str | None) -> list[dict]:
    """The merged policy table for one merchant, in policy_table() shape plus an "override" column."""
    from . import policy  # policy imports nothing from here; kept lazy so either module can be imported first
    return policy.policy_table(overrides=get(merchant_id))


# ---- writing overrides with evidence (operator actions, docs/web.md) -----------------------------

class OverrideRefused(ValueError):
    """A write the rules do not allow: no evidence, a field the schema does not know, or a change
    that would make the agent LESS conservative than the table without proposal-grade evidence."""


def override_path(merchant_id: str | None) -> Path:
    """merchants/<merchant_id>.json, or _default.json for None / "all"."""
    mid = (merchant_id or "").strip()
    if not mid or mid == ALL_MERCHANTS:
        return merchants_dir() / DEFAULT_FILE
    if mid.startswith("_") or "/" in mid or "\\" in mid or mid in (".", ".."):
        raise OverrideRefused(f"merchant id {mid!r} cannot name an override file (underscore files are skipped; no path separators)")
    return merchants_dir() / f"{mid}.json"


def _read_raw(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise MerchantConfigError(f"{path}: not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise MerchantConfigError(f"{path}: field (root): must be a JSON object")
    return data


def _write_raw(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def less_conservative_than_table(class_name: str, fields: dict) -> list[str]:
    """The fields in `fields` that would make the agent act sooner, more often or louder than
    app/policy.py's table for this class: a shorter delay, more attempts, a nudge the table does
    not send. Empty when every field is at least as conservative."""
    from . import policy  # lazy, as effective_table does
    try:
        entry = policy.POLICY[FailureClass(class_name)]
    except (KeyError, ValueError):
        return []
    worse = []
    if "delay_seconds" in fields and fields["delay_seconds"] is not None and int(fields["delay_seconds"]) < int(entry["delay_seconds"]):
        worse.append(f"delay_seconds {fields['delay_seconds']} < table {entry['delay_seconds']}")
    if "max_attempts" in fields and fields["max_attempts"] is not None and int(fields["max_attempts"]) > int(entry["max_attempts"]):
        worse.append(f"max_attempts {fields['max_attempts']} > table {entry['max_attempts']}")
    if fields.get("nudge") is True and not entry.get("nudge", False):
        worse.append("nudge on where the table sends none")
    return worse


def write_override(merchant_id: str | None, class_name: str, fields: dict, *, evidence: str, actor: str,
                   proposal_n: tuple[int, int] | None = None, when: datetime | None = None) -> Path:
    """Merge `fields` into merchants/<merchant_id>.json (or _default.json for None/"all") under
    classes[class_name], validated through the same schema a hand-edited file passes, and append
    a _history entry {when, actor, evidence, class, previous} so rollback_override can undo it.
    Refused (OverrideRefused): no evidence; a field ClassOverride does not know; a value that would
    make the agent less conservative than the table unless `proposal_n` says both insights buckets
    had n >= 30 (the evidence rule); a malformed result is MerchantConfigError and nothing is written.
    Reloads the registry so the running process sees the file."""
    if not (evidence or "").strip():
        raise OverrideRefused("refused: an override needs evidence (why this change, from which report); a bare write "
                              "is what the code-reviewed table exists to prevent")
    if not (actor or "").strip():
        raise OverrideRefused("refused: an override needs an actor name")
    if class_name not in {c.value for c in FailureClass}:
        raise OverrideRefused(f"unknown failure class {class_name!r}")
    allowed = set(ClassOverride.model_fields)
    unknown = sorted(set(fields) - allowed)
    if unknown or not fields:
        raise OverrideRefused(f"fields must be a non-empty subset of {', '.join(sorted(allowed))}; got {sorted(fields) or 'nothing'}")
    worse = less_conservative_than_table(class_name, fields)
    if worse:
        if proposal_n is None or min(proposal_n) < 30:
            raise OverrideRefused(f"refused: {'; '.join(worse)} makes the agent less conservative than the table; only an "
                                  f"insights proposal with n >= 30 in both buckets may do that (have {proposal_n or 'no proposal'})")
    path = override_path(merchant_id)
    data = _read_raw(path)
    classes = data.setdefault("classes", {})
    if not isinstance(classes, dict):
        raise MerchantConfigError(f"{path}: field classes: must be an object")
    previous = classes.get(class_name)
    merged = dict(previous or {})
    merged.update(fields)
    classes[class_name] = merged
    history = data.setdefault(HISTORY_KEY, [])
    history.append({"when": (when or datetime.utcnow()).replace(microsecond=0).isoformat() + "Z", "actor": actor.strip(),
                    "evidence": evidence.strip(), "class": class_name, "fields": dict(fields),
                    "previous": dict(previous) if previous is not None else None,
                    "proposal_n": list(proposal_n) if proposal_n else None})
    try:
        MerchantPolicy.model_validate({k: v for k, v in data.items() if k != HISTORY_KEY})
    except ValidationError as exc:
        raise MerchantConfigError(_format_validation_error(path, exc)) from exc
    _write_raw(path, data)
    reload(merchants_dir())
    return path


def rollback_override(merchant_id: str | None, class_name: str) -> Path:
    """Undo the most recent write_override for this class in this file: restore its `previous`
    (deleting the class entry when there was none) and drop that history entry. Refused when the
    file or the class has no history to roll back."""
    path = override_path(merchant_id)
    data = _read_raw(path)
    history = data.get(HISTORY_KEY) or []
    idx = next((i for i in range(len(history) - 1, -1, -1) if history[i].get("class") == class_name), None)
    if idx is None:
        raise OverrideRefused(f"{path.name}: no recorded write for {class_name} to roll back")
    entry = history.pop(idx)
    classes = data.setdefault("classes", {})
    if entry.get("previous") is None:
        classes.pop(class_name, None)
    else:
        classes[class_name] = dict(entry["previous"])
    if not classes:
        data.pop("classes", None)
    if not history:
        data.pop(HISTORY_KEY, None)
    try:
        MerchantPolicy.model_validate({k: v for k, v in data.items() if k != HISTORY_KEY})
    except ValidationError as exc:
        raise MerchantConfigError(_format_validation_error(path, exc)) from exc
    _write_raw(path, data)
    reload(merchants_dir())
    return path


def main(argv: list[str] | None = None) -> int:
    """python -m app.merchants apply --proposal ID (--merchant ID | --all) --actor NAME [--min-samples N]
       python -m app.merchants rollback (--merchant ID | --all) --class CLASS"""
    import argparse
    import sys
    p = argparse.ArgumentParser(prog="python -m app.merchants", description="write or roll back an override with evidence")
    sub = p.add_subparsers(dest="command")
    ap = sub.add_parser("apply", help="apply an insights proposal (by id, as /insights and `python -m app.insights` show it)")
    ap.add_argument("--proposal", required=True, metavar="ID")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--merchant", metavar="ID")
    g.add_argument("--all", action="store_true", help="every merchant (_default.json)")
    ap.add_argument("--actor", required=True, metavar="NAME")
    ap.add_argument("--min-samples", type=int, default=30, metavar="N", help="the report's threshold (writing still needs 30)")
    rb = sub.add_parser("rollback", help="restore the previous entry of the last write for a class")
    g2 = rb.add_mutually_exclusive_group(required=True)
    g2.add_argument("--merchant", metavar="ID")
    g2.add_argument("--all", action="store_true")
    rb.add_argument("--class", dest="class_name", required=True, metavar="CLASS")
    args = p.parse_args(argv)
    if not args.command:
        p.print_help()
        return 1
    merchant = None if args.all else args.merchant
    try:
        if args.command == "rollback":
            path = rollback_override(merchant, args.class_name)
            print(f"rolled back {args.class_name} in {path}")
            return 0
        from . import actions, db  # lazy: actions imports this module
        db.init_db()
        session = db.session()
        try:
            result = actions.apply_proposal(session, args.proposal, args.actor, merchant_id=merchant, min_samples=args.min_samples)
        finally:
            session.close()
        print(result["message"], file=sys.stdout if result["ok"] else sys.stderr)
        return 0 if result["ok"] else 1
    except (OverrideRefused, MerchantConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
