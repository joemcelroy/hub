import os
import typer
import asyncio
from typing import Annotated, Optional
from pathlib import Path

from tomllib import load as load_toml
from tomli_w import dump as dump_toml
from urllib import request

import subprocess

from .github import download_github_path


app = typer.Typer(no_args_is_help=True, add_completion=False)


class PyProject:
    def __init__(self, data: dict, path: str | None):
        self.data = data
        self.path = path

    @classmethod
    def load(cls, path: str):
        with open(path, "rb") as f:
            data = load_toml(f)
        return cls(data, path)

    @classmethod
    def from_url(cls, url: str):
        try:
            with request.urlopen(url) as f:
                data = load_toml(f)
            return cls(data, None)
        except request.HTTPError as e:
            raise ValueError(f"Consider updating your GITHUB_TOKEN") from e

    def save(self):
        if self.path is None:
            raise ValueError("Cannot save a PyProject that was not loaded from a file")
        with open(self.path, "wb") as f:
            dump_toml(self.data, f)

    def _get_langserve_path_dict(self):
        return self.data["tool"].get("langserve", {}).get("paths", {})

    def _set_langserve_path_dict(self, path_dict):
        langserve = self.data["tool"].get("langserve", {})
        langserve["paths"] = path_dict
        self.data["tool"]["langserve"] = langserve

    def is_langserve(self):
        return "langserve" in self.data["tool"]

    def add_langserve_path(self, path: str, module: tuple[str, str]):
        paths = self._get_langserve_path_dict()
        paths[path] = module
        self._set_langserve_path_dict(paths)

    def remove_langserve_path(self, path: str):
        paths = self._get_langserve_path_dict()
        paths.pop(path)
        self._set_langserve_path_dict(paths)

    def get_langserve_paths(self):
        return self._get_langserve_path_dict()

    def get_langserve_export(self):
        module = self.data["tool"].get("langserve", {}).get("export_module")
        if module is None:
            print(self.data)
            raise ValueError(
                "No module name was exported at `tool.langserve.export_module`"
            )
        attr = self.data["tool"].get("langserve", {}).get("export_attr")
        if attr is None:
            raise ValueError(
                "No attr name was exported at `tool.langserve.export_attr`"
            )
        return module, attr


def _load_pyproject():
    curr_proj_pyproject = Path("pyproject.toml")
    return PyProject.load(curr_proj_pyproject)


@app.command()
def list():
    curr_data = _load_pyproject()
    langserve = curr_data["tool"].get("langserve", {})
    for k, v in langserve.items():
        typer.echo(f"{k} -> {v}")


@app.command()
def remove(path: str):
    curr_data = _load_pyproject()
    curr_data.remove_langserve_path(path)
    curr_data.save()


def _package_to_poetry(package: str):
    package_type = "hub"
    if package.startswith(".") or package.startswith("/"):
        # package_type = "path"
        return package

    # if it's hub, have to add base path
    return (
        f"git+https://github.com/langchain-ai/langserve-hub.git#subdirectory={package}"
    )


def _package_to_pyproject(package: str):
    if package.startswith(".") or package.startswith("/"):
        # package_type = "path"
        return f"{package}/pyproject.toml"
    tokenparam = (
        ""
        if not os.environ.get("GITHUB_TOKEN")
        else f"?token={os.environ['GITHUB_TOKEN']}"
    )
    return f"https://raw.githubusercontent.com/langchain-ai/langserve-hub/main/{package}/pyproject.toml{tokenparam}"


@app.command()
def add(
    package: Annotated[
        str, typer.Argument(help="The package in LangServe Hub like `simple/pirate`")
    ],
    path: Annotated[
        Optional[str],
        typer.Argument(help="The api path to mount the chain at like `/pirate`"),
    ] = None,
):
    # get pyproject
    package_pyproject_url = _package_to_pyproject(package)
    package_pyproject = PyProject.from_url(package_pyproject_url)

    # validate it's langserve-compatible
    if not package_pyproject.is_langserve():
        raise ValueError(
            f"Package {package} does not have a `tool.langserve` section in its pyproject.toml"
        )

    package_path = _package_to_poetry(package)
    # poetry add it
    typer.echo(f"Adding {package}")
    subprocess.run(f"poetry add {package_path}", shell=True)

    # validate it's a langserve package

    # poetry install it from git
    api_path = path or f"/{package}"

    pyproject = _load_pyproject()
    pyproject.add_langserve_path(api_path, package_pyproject.get_langserve_export())

    pyproject.save()


@app.command()
def download(
    package: Annotated[
        str, typer.Argument(help="The package in LangServe Hub like `simple/pirate`")
    ],
    localpath: Annotated[str, typer.Argument(help="The local path to download to")],
):
    asyncio.run(download_github_path(package, localpath))
    print(f"Successfully downloaded {package} to {localpath}")


@app.command()
def serve():
    typer.echo("Validating dependencies and installing missing ones")
    subprocess.run("poetry install", shell=True)
    typer.echo("Successfully installed missing dependencies")

    curr_data = _load_pyproject()

    from fastapi import FastAPI
    import uvicorn
    from langserve import add_routes

    fastapp = FastAPI()

    for k, v in curr_data.get_langserve_paths().items():
        module, attr = v
        mod = __import__(module)
        chain = getattr(mod, attr)
        add_routes(fastapp, chain, path=k)

    print("Check out the docs at http://localhost:8000/docs")

    uvicorn.run(fastapp, host="localhost", port=8000)


if __name__ == "__main__":
    app()
