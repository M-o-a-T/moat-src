# command line interface
# pylint: disable=missing-module-docstring

import io
import logging
import subprocess
from collections import defaultdict
from configparser import RawConfigParser
from pathlib import Path

import asyncclick as click
import git
import tomlkit
from moat.util import P, add_repr, attrdict, make_proc, yload, yprint
from packaging.requirements import Requirement

logger = logging.getLogger(__name__)


class Repo(git.Repo):
    """Amend git.Repo with submodule and tag caching"""

    moat_tag = None

    def __init__(self, root, *a, **k):
        super().__init__(*a, **k)
        self._subrepo_cache = {}
        self._commit_tags = defaultdict(list)
        self._commit_topo = {}

        for t in self.tags:
            self._commit_tags[t.commit].append(t)

        if root is None:
            self.moat_name = "moat"
        else:
            self.moat_name = "moat-" + self.working_dir[len(root.working_dir) + 1 :].replace(
                "/", "-"
            )

    def subrepos(self, root=None, recurse=True):
        """List subrepositories (and cache them)."""

        if root is None:
            root = self

        for r in self.submodules:
            try:
                res = self._subrepo_cache[r.path]
            except KeyError:
                p = Path(self.working_dir) / r.path
                self._subrepo_cache[r.path] = res = Repo(root, p)
            if recurse:
                yield from res.subrepos(root)
            yield res

    def commits(self, ref=None):
        """Iterate over topo sort of commits following @ref, or HEAD"""
        if ref is None:
            ref = self.head.commit
        try:
            res = self._commit_topo[ref]
        except KeyError:
            visited = set()
            res = []

            def _it(c):
                return iter(sorted(c.parents, key=lambda x: x.committed_date))

            work = [(ref, _it(ref))]

            while work:
                c, gen = work.pop()
                visited.add(c)
                for n in gen:
                    if n not in visited:
                        work.append((c, gen))
                        work.append((n, _it(n)))
                        break
                else:
                    res.append(c)
            self._commit_topo[ref] = res

        n = len(res)
        while n:
            n -= 1
            yield res[n]

    def tagged(self, c=None) -> str:
        """Return a commit's tag.
        Defaults to the head commit.
        Returns None if no tag, raises ValueError if more than one is found.
        """
        if c is None:
            c = self.head.commit
        if c not in self._commit_tags:
            return None
        tt = self._commit_tags[c]
        if len(tt) > 1:
            raise ValueError(f"multiple tags: {tt}")
        return tt[0]


@click.group(short_help="Manage MoaT itself")
async def cli():
    """
    This collection of commands is useful for managing and building MoaT itself.
    """
    pass  # pylint: disable=unnecessary-pass


def fix_deps(deps: list[str], tags: dict[str, str]) -> bool:
    """Adjust dependencies"""
    work = False
    for i, dep in enumerate(deps):
        r = Requirement(dep)
        if r.name in tags:
            dep = f"{r.name} ~= {tags[r.name]}"
            if deps[i] != dep:
                deps[i] = dep
                work = True
    return work


def run_tests(repo: Repo) -> bool:
    """Run tests (i.e., 'tox') in this repository."""

    tst = Path(repo.working_dir).joinpath("Makefile")
    if not tst.is_file():
        # No Makefile. Assume it's OK.
        return True
    try:
        print("\n*** Testing:", repo.working_dir)
        # subprocess.run(["python3", "-mtox"], cwd=repo.working_dir, check=True)
        subprocess.run(["make", "test"], cwd=repo.working_dir, check=True)
    except subprocess.CalledProcessError:
        return False
    else:
        return True


class Replace:
    """Encapsulates a series of string replacements."""

    def __init__(self, **kw):
        self.changes = kw

    def __call__(self, s):
        if isinstance(s, str):
            for k, v in self.changes.items():
                s = s.replace(k, v)
        return s


_l_t = (list, tuple)


def default_dict(a, b, c, cls=dict, repl=lambda x: x) -> dict:
    """
    Returns a dict with all keys+values of all dict arguments.
    The first found value wins.

    This operation is recursive and non-destructive.

    Args:
    cls (type): a class to instantiate the result with. Default: dict.
            Often used: :class:`attrdict`.
    """
    keys = defaultdict(list)
    mod = False

    for kv in a, b, c:
        if kv is None:
            continue
        for k, v in kv.items():
            keys[k].append(v)

    for k, v in keys.items():
        va = a.get(k, None)
        vb = b.get(k, None)
        vc = c.get(k, None)
        if isinstance(va, str) and va == "DELETE":
            if vc is None:
                try:
                    del b[k]
                except KeyError:
                    pass
                else:
                    mod = True
                continue
            else:
                b[k] = {} if isinstance(vc, dict) else [] if isinstance(vc, _l_t) else 0
                vb = b[k]
            va = None
        if isinstance(va, dict) or isinstance(vb, dict) or isinstance(vc, dict):
            if vb is None:
                b[k] = {}
                vb = b[k]
                mod = True
            mod = default_dict(va or {}, vb, vc or {}, cls=cls, repl=repl) or mod
        elif isinstance(va, _l_t) or isinstance(vb, _l_t) or isinstance(vc, _l_t):
            if vb is None:
                b[k] = []
                vb = b[k]
                mod = True
            if va:
                for vv in va:
                    vv = repl(vv)
                    if vv not in vb:
                        vb.insert(0, vv)
                        mod = True
            if vc:
                for vv in vc:
                    vv = repl(vv)
                    if vv not in vb:
                        vb.insert(0, vv)
                        mod = True
        else:
            v = repl(va) or vb or repl(vc)
            if vb != v:
                b[k] = v
                mod = True
    return mod


def is_clean(repo: Repo, skip: bool = True) -> bool:
    """Check if this repository is clean."""
    skips = " Skipping." if skip else ""
    if repo.head.is_detached:
        print(f"{repo.working_dir}: detached.{skips}")
        return False
    if repo.head.ref.name not in {"main", "moat"}:
        print(f"{repo.working_dir}: on branch {repo.head.ref.name}.{skips}")
        return False
    elif repo.is_dirty(index=True, working_tree=True, untracked_files=False, submodules=False):
        print(f"{repo.working_dir}: Dirty.{skips}")
        return False
    return True


def _mangle(proj, path, mangler):
    try:
        for k in path[:-1]:
            proj = proj[k]
        k = path[-1]
        v = proj[k]
    except KeyError:
        return
    v = mangler(v)
    proj[k] = v


def decomma(proj, path):
    """comma-delimited string > list"""
    _mangle(proj, path, lambda x: x.split(","))


def encomma(proj, path):
    """list > comma-delimited string"""
    _mangle(proj, path, lambda x: ",".join(x))  # pylint: disable=unnecessary-lambda


def apply_templates(repo):
    """
    Apply templates to this repo.
    """
    commas = (
        P("tool.tox.tox.envlist"),
        P("tool.pylint.messages_control.enable"),
        P("tool.pylint.messages_control.disable"),
    )

    rpath = Path(repo.working_dir)
    if rpath.parent.name == "lib" or rpath.parent.parent.name == "moat":
        rname = f"{rpath.parent.name}-{rpath.name}"
        rdot = f"{rpath.parent.name}.{rpath.name}"
        rpath = f"{rpath.parent.name}/{rpath.name}"
    else:
        rname = str(rpath.name)
        rdot = str(rpath.name)
        rpath = str(rpath.name)
    repl = Replace(
        SUBNAME=rname,
        SUBDOT=rdot,
        SUBPATH=rpath,
    )
    pt = (Path(__file__).parent / "_templates").joinpath
    pr = Path(repo.working_dir).joinpath
    with pt("pyproject.forced.yaml").open("r") as f:
        t1 = yload(f)
    with pt("pyproject.default.yaml").open("r") as f:
        t2 = yload(f)
    try:
        with pr("pyproject.toml").open("r") as f:
            proj = tomlkit.load(f)
        try:
            tx = proj["tool"]["tox"]["legacy_tox_ini"]
        except KeyError:
            pass
        else:
            txp = RawConfigParser()
            txp.read_string(tx)
            td = {}
            for k, v in txp.items():
                td[k] = ttd = dict()
                for kk, vv in v.items():
                    if isinstance(vv, str) and vv[0] == "\n":
                        vv = [x.strip() for x in vv.strip().split("\n")]
                    ttd[kk] = vv
            proj["tool"]["tox"] = td

        for p in commas:
            decomma(proj, p)

    except FileNotFoundError:
        proj = tomlkit.TOMLDocument()
    mod = default_dict(t1, proj, t2, repl=repl, cls=tomlkit.items.Table)
    try:
        proc = proj["tool"]["moat"]["fixup"]
    except KeyError:
        p = proj
    else:
        del proj["tool"]["moat"]["fixup"]
        proc = make_proc(proc, ("toml",), f"{pr('pyproject.toml')}:tool.moat.fixup")
        s1 = proj.as_string()
        proc(proj)
        s2 = proj.as_string()
        mod |= s1 != s2

    if mod:
        for p in commas:
            encomma(proj, p)

        try:
            tx = proj["tool"]["tox"]
        except KeyError:
            pass
        else:
            txi = io.StringIO()
            txp = RawConfigParser()
            for k, v in tx.items():
                if k != "DEFAULT":
                    txp.add_section(k)
                for kk, vv in v.items():
                    if isinstance(vv, (tuple, list)):
                        vv = "\n   " + "\n   ".join(str(x) for x in vv)
                    txp.set(k, kk, vv)
            txp.write(txi)
            txi = txi.getvalue()
            txi = "\n" + txi.replace("\n\t", "\n ")
            proj["tool"]["tox"] = dict(
                legacy_tox_ini=tomlkit.items.String.from_raw(
                    txi, type_=tomlkit.items.StringType.MLB
                )
            )

        (Path(repo.working_dir) / "pyproject.toml").write_text(proj.as_string())
        repo.index.add(Path(repo.working_dir) / "pyproject.toml")

    mkt = repl(pt("Makefile").read_text())
    try:
        mk = pr("Makefile").read_text()
    except FileNotFoundError:
        mk = ""
    if mkt != mk:
        pr("Makefile").write_text(mkt)
        repo.index.add(pr("Makefile"))

    tst = pr("tests")
    if not tst.is_dir():
        tst.mkdir()
    for n in tst.iterdir():
        if n.name.startswith("test_"):
            break
    else:
        tp = pt("test_basic_py").read_text()
        tb = pr("tests") / "test_basic.py"
        tb.write_text(repl(tp))
        repo.index.add(tb)

    try:
        with pr(".gitignore").open("r") as f:
            ign = f.readlines()
    except FileNotFoundError:
        ign = []
    o = len(ign)
    with pt("gitignore").open("r") as f:
        for li in f:
            if li not in ign:
                ign.append(li)
    if len(ign) != o:
        with pr(".gitignore").open("w") as f:
            for li in ign:
                f.write(li)
        repo.index.add(pr(".gitignore"))


@cli.command(
    epilog="""\
By default, changes amend the HEAD commit if the text didn't change.
"""
)
@click.option("-A", "--amend", is_flag=True, help="Fix previous commit (DANGER)")
@click.option("-N", "--no-amend", is_flag=True, help="Don't fix prev commit even if same text")
@click.option("-D", "--no-dirty", is_flag=True, help="don't check for dirtiness (DANGER)")
@click.option("-C", "--no-commit", is_flag=True, help="don't commit")
@click.option("-s", "--skip", type=str, multiple=True, help="skip this repo")
@click.option(
    "-m",
    "--message",
    type=str,
    help="commit message if changed",
    default="Update from MoaT template",
)
@click.option("-o", "--only", type=str, multiple=True, help="affect only this repo")
async def setup(no_dirty, no_commit, skip, only, message, amend, no_amend):
    """
    Set up projects using templates.

    Default: amend if the text is identical and the prev head isn't tagged.
    """
    repo = Repo(None)
    skip = set(skip)
    if only:
        repos = (Repo(repo, x) for x in only)
    else:
        repos = (x for x in repo.subrepos() if x.moat_name[5:] not in skip)

    for r in repos:
        if not is_clean(r, not no_dirty):
            if not no_dirty:
                continue

        tst = Path(r.working_dir).joinpath("pyproject.toml")
        if tst.is_file():
            apply_templates(r)
        else:
            logger.info("%s: no pyproject.toml file. Skipping.")
            continue

        if no_commit:
            continue
        if r.is_dirty(index=True, working_tree=False, untracked_files=False, submodules=False):
            if no_amend or r.tagged():
                a = False
            elif amend:
                a = True
            else:
                a = r.head.commit.message == message

            if a:
                p = r.head.commit.parents
            else:
                p = (r.head.commit,)
            r.index.commit(message, parent_commits=p)


@cli.command()
@click.option("-P", "--no-pypi", is_flag=True, help="don't push to PyPi")
@click.option("-D", "--no-deb", is_flag=True, help="don't debianize")
@click.option("-d", "--deb", type=str, help="Debian archive to push to (from dput.cfg)")
@click.option("-o", "--only", type=str, multiple=True, help="affect only this repo")
@click.option("-s", "--skip", type=str, multiple=True, help="skip this repo")
async def publish(no_pypi, no_deb, skip, only, deb):
    """
    Publish modules to PyPi and/or Debian.
    """
    repo = Repo(None)
    skip = set(skip)
    if only:
        repos = (Repo(repo, x) for x in only)
    else:
        repos = (x for x in repo.subrepos() if x.moat_name[5:] not in skip)

    for r in repos:
        if not no_deb:
            print(r.working_dir)
            args = ["-d", deb] if deb else []
            subprocess.run(["merge-to-deb"] + args, cwd=r.working_dir, check=True)

    for r in repos:
        if not no_pypi:
            print(r.working_dir)
            subprocess.run(["make", "pypi"], cwd=r.working_dir, check=True)


@cli.command()
@click.option("-T", "--no-test", is_flag=True, help="Skip testing")
@click.option(
    "-v",
    "--version",
    type=(str, str),
    multiple=True,
    help="Update external dep version",
)
@click.option("-C", "--no-commit", is_flag=True, help="don't commit")
@click.option("-D", "--no-dirty", is_flag=True, help="don't check for dirtiness (DANGER)")
@click.option("-c", "--cache", is_flag=True, help="don't re-test if unchanged")
async def build(version, no_test, no_commit, no_dirty, cache):
    """
    Rebuild all modified packages.
    """
    bad = False
    repo = Repo(None)
    tags = dict(version)
    skip = set()
    heads = attrdict()

    if repo.is_dirty(index=True, working_tree=True, untracked_files=False, submodules=False):
        print("Please commit top-level changes and try again.")
        return

    if cache:
        cache = Path(".tested.yaml")
        try:
            heads = yload(cache, attr=True)
        except FileNotFoundError:
            pass

    for r in repo.subrepos():
        if not is_clean(r, not no_dirty):
            bad = True
            if not no_dirty:
                skip.add(r)
                continue

        if not no_test and heads.get(r.moat_name, "") != r.commit().hexsha and not run_tests(r):
            print("FAIL", r.moat_name)
            bad = True
            break

        if r.is_dirty(index=True, working_tree=True, untracked_files=True, submodules=False):
            print("DIRTY", r.moat_name)
            if r.moat_name != "src":
                bad = True
            continue

        heads[r.moat_name] = r.commit().hexsha
        t = r.tagged(r.head.commit)
        if t is None:
            for c in r.commits():
                t = r.tagged(c)
                if t is not None:
                    break
            else:
                print("NOTAG", t, r.moat_name)
                bad = True
                continue
            print("UNTAGGED", t, r.moat_name)
            xt, t = t.name.rsplit(".", 1)
            t = f"{xt}.{int(t)+1}"
            # t = r.create_tag(t)
            # do not create the tag yet
        else:
            print("TAG", t, r.moat_name)
        tags[r.moat_name] = t

    if cache:
        with cache.open("w") as f:
            # always write cache file
            yprint(heads, stream=f)
    if bad:
        print("No work done. Fix and try again.")
        return

    dirty = set()

    check = check1 = True

    while check:
        check = False

        # Next: fix versioned dependencies
        for r in repo.subrepos():
            if r in skip:
                continue
            p = Path(r.working_dir) / "pyproject.toml"
            if not p.is_file():
                # bad=True
                print("Skip:", r.working_dir)
                continue
            with p.open("r") as f:
                pr = tomlkit.load(f)

            work = False
            try:
                deps = pr["project"]["dependencies"]
            except KeyError:
                pass
            else:
                work = fix_deps(deps, tags) | work
            try:
                deps = pr["project"]["optional_dependencies"]
            except KeyError:
                pass
            else:
                for v in deps.values():
                    work = fix_deps(v, tags) | work

            if check1:
                if r.is_dirty(
                    index=True, working_tree=True, untracked_files=False, submodules=True
                ):
                    for rr in r.subrepos(recurse=False):
                        r.git.add(rr.working_dir)
                    work = True

            if work:
                p.write_text(pr.as_string())
                r.index.add(p)
                dirty.add(r)
                t = tags[r.moat_name]
                if not isinstance(t, str):
                    xt, t = t.name.rsplit(".", 1)
                    t = f"{xt}.{int(t)+1}"
                    tags[r.moat_name] = t
                check = True

        check1 = False

    if bad:
        print("Partial work done. Fix and try again.")
        return

    if not no_commit:
        for r in dirty:
            r.index.commit("Update MoaT requirements")

        if not repo.is_dirty(
            index=True, working_tree=True, untracked_files=False, submodules=True
        ):
            print("No changes.")
            return

        for r in repo.subrepos():
            t = tags[r.moat_name]
            if isinstance(t, str):
                r.create_tag(t)

        for r in repo.subrepos(recurse=False):
            repo.git.add(r.working_dir)
        repo.index.commit("Update")

        for c in repo.commits():
            t = repo.tagged(c)
            if t is not None:
                break
        else:
            print("NO TAG", repo.moat_name)
            return

        xt, t = t.name.rsplit(".", 1)
        t = f"{xt}.{int(t)+1}"

        print("New:", t)
        repo.create_tag(t)


add_repr(tomlkit.items.String)
add_repr(tomlkit.items.Integer)
add_repr(tomlkit.items.Bool, bool)
add_repr(tomlkit.items.MutableMapping)
add_repr(tomlkit.items.MutableSequence)
