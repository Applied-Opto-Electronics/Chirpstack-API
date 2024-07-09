from argparse import ArgumentParser
from asyncio import Task, create_task, gather, run
from copy import deepcopy
from enum import StrEnum
from os import getcwd, makedirs
from os.path import exists, isdir, join
from pathlib import Path
from shutil import rmtree
from subprocess import run as run_command
from typing import List, Optional

from aiohttp import ClientSession
from pydantic import BaseModel, Field, TypeAdapter


class GHRepo(BaseModel):
    username: str
    repo: str


class Typ(StrEnum):
    DIR = "dir"
    FILE = "file"


class Content(BaseModel):
    path: str
    type: Typ = Field(..., alias="type")
    content: bytes | None = None


async def download_content(session: ClientSession, ghr: GHRepo, path: str) -> Content:
    async with session.get(
        f"https://cdn.jsdelivr.net/gh/{ghr.username}/{ghr.repo}/{path}"
    ) as _t:
        return Content(path=path, type=Typ.FILE, content=await _t.read())


class GHContent(Content):
    name: str
    size: int
    sha: str

    async def get_content(self, session: ClientSession, ghr: GHRepo) -> None:
        if self.type == Typ.FILE:
            self.content = (
                await download_content(session=session, ghr=ghr, path=self.path)
            ).content


async def get_content(
    session: ClientSession, ghr: GHRepo, path: str, commit_hash: Optional[str] = None
) -> List[GHContent]:
    path = path.strip("/")
    ref = f"?ref={commit_hash}" if commit_hash is not None else ""

    async with session.get(
        f"https://api.github.com/repos/{ghr.username}/{ghr.repo}/contents/{path}" + ref
    ) as _t:
        response = TypeAdapter(List[GHContent]).validate_python(await _t.json())

        tasks: List[Task] = []

        for z in response:
            if z.type == Typ.DIR:
                tasks.append(
                    create_task(
                        get_content(
                            session=session,
                            ghr=ghr,
                            path=z.path,
                            commit_hash=commit_hash,
                        )
                    )
                )

        result = await gather(*tasks)
        for z in result:
            response.extend(z)

        tasks.clear()
        tasks.extend(
            [create_task(z.get_content(session=session, ghr=ghr)) for z in response]
        )
        _ = await gather(*tasks)

        return response


def left_remove(s: str, rem: str) -> str:
    if s.startswith(rem):
        s = s[len(rem) :]
    return s


def save_to_folder(og_path: str | None, save_to: str, contents: List[Content]) -> None:
    contents.sort(key=lambda z: len(z.path), reverse=False)
    for _t in contents:
        z = deepcopy(_t)

        if og_path is not None:
            og_path = og_path.strip("/")
            z.path = left_remove(z.path, og_path)

        z.path = join(getcwd(), save_to.strip("/"), z.path.strip("/"))

        if z.type == Typ.FILE:
            makedirs(Path(z.path).parent, exist_ok=True)
            if z.content is not None:
                with open(z.path, "w+") as fp:
                    fp.write(z.content.decode("utf-8"))


async def main(commit_hash: Optional[str]):
    chirpstack = GHRepo(username="chirpstack", repo="chirpstack")
    googleapis = GHRepo(username="googleapis", repo="googleapis")
    protobuf = GHRepo(username="protocolbuffers", repo="protobuf")

    async with ClientSession() as session:
        og_path = "api/proto"

        contents = await get_content(
            session=session, ghr=chirpstack, path=og_path, commit_hash=commit_hash
        )

        save_to_folder(
            og_path=og_path,
            save_to=chirpstack.repo,
            contents=TypeAdapter(List[Content]).validate_python(contents),
        )

        tasks1 = [
            create_task(
                download_content(
                    session=session,
                    ghr=googleapis,
                    path=z,
                )
            )
            for z in ["google/api/annotations.proto", "google/api/http.proto"]
        ]
        tasks2 = [
            create_task(
                download_content(
                    session=session,
                    ghr=protobuf,
                    path=z,
                )
            )
            for z in [
                "src/google/protobuf/descriptor.proto",
                "src/google/protobuf/duration.proto",
                "src/google/protobuf/empty.proto",
                "src/google/protobuf/struct.proto",
                "src/google/protobuf/timestamp.proto",
            ]
        ]

        save_to_folder(
            og_path=None, save_to=googleapis.repo, contents=await gather(*tasks1)
        )
        save_to_folder(
            og_path=None, save_to=protobuf.repo, contents=await gather(*tasks2)
        )

        def rmdir(dir_path: str) -> None:
            if exists(dir_path) and isdir(dir_path):
                rmtree(dir_path)

        generate_to = "chirpstack_api"

        rmdir(generate_to)
        makedirs(generate_to)

        paths = " ".join(
            list(
                sorted(
                    list(
                        map(
                            lambda z: left_remove(z.path, og_path).strip("/"),
                            filter(lambda zz: zz.type == Typ.FILE, contents),
                        )
                    )
                )
            )
        )

        command = f"python -m grpc_tools.protoc -I=./googleapis -I=./protobuf/src -I=./chirpstack --python_betterproto_out={generate_to} {paths}"

        # --pre & 3.11.6
        run_command(command, shell=True)

        rmdir(chirpstack.repo)
        rmdir(googleapis.repo)
        rmdir(protobuf.repo)


if __name__ == "__main__":
    parser = ArgumentParser(description="Generate protocol buffers using betterproto")
    parser.add_argument(
        "--commit_hash",
        type=str,
        required=False,
        help="The commit hash of chirpstack to use",
    )

    args = parser.parse_args()

    run(main(commit_hash=args.commit_hash))
