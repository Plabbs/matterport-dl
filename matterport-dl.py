#!/usr/bin/env python3
# ruff: noqa: E722

"""
Downloads virtual tours from matterport.
Usage is either running this program with the URL/pageid as an argument or calling the initiateDownload(URL/pageid) method.
"""

from __future__ import annotations
import urllib.parse
from curl_cffi import requests
from enum import Enum
import asyncio
import aiofiles
import json
import threading
import urllib.request
from urllib.parse import urlparse
import pathlib
import re
import os
import hashlib
import html
import platform

import shutil
import sys
from typing import Any, ClassVar, cast
from dataclasses import dataclass

import logging
import collections
import socket
import time
from functools import partial
from tqdm import tqdm as std_tqdm

tqdm = partial(std_tqdm, smoothing=0.1)
from http.server import HTTPServer, SimpleHTTPRequestHandler, ThreadingHTTPServer
import decimal
import signal

signal.signal(signal.SIGINT, signal.SIG_DFL)  # quiet control + c

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


BASE_MATTERPORTDL_DIR = pathlib.Path(__file__).resolve().parent
SCRIPT_NAME = os.path.basename(sys.argv[0])
MAX_CONCURRENT_REQUESTS = 20  # cffi will make sure no more than this many curl workers are used at once
MAX_CONCURRENT_TASKS = 64  # while we could theoretically leave this unbound just relying on MAX_CONCURRENT_REQESTS there is little reason to spawn a million tasks at once

BASE_MATTERPORT_DOMAIN = "matterport.com"
CHINA_MATTERPORT_DOMAIN = "matterportvr.cn"
MAIN_SHOWCASE_FILENAME = ""  # the filename for the main showcase runtime
# Matterport uses various access keys for a page, when the primary key doesnt work we try some other ones,  note a single model can have 1400+ unique access keys not sure which matter vs not


dirsMadeCache: dict[str, bool] = {}
THIS_MODEL_ROOT_DIR: str
SERVED_BASE_URL: str  # url we are serving from ie http://127.0.0.1:8080
MODEL_IS_DEFURNISHED = False  # defurnished models can be accessed directly but have some quarks eventually will add to initial dl
BASE_MODEL_ID = ""  # normally this is the model id we are downloading unless defurnished
SWEEP_DO_4K = True  # assume 4k  by default

AccessKeyType = Enum("AccessKeyType", ["LeaveKeyAlone", "PrimaryKey", "MAIN_PAGE_GENERIC_KEY", "MAIN_PAGE_DAM_50K", "FILES2_BASE_URL_KEY", "FILES3_TEMPLATE_KEY", "SWEEP_KEY", "GRAPH_MODEL_VIEW_PREFETCH"])  # sweep key primarily used for defurnished, GRAPH_MODEL_VIEW_PREFETCH is only used for attachments


# if no git revision fall back to our sha
def self_sha():
    with open(pathlib.Path(__file__).resolve(), "rb") as f:
        return hashlib.file_digest(f, "sha1").hexdigest()


# modified from https://gist.github.com/pkienzle/5e13ec07077d32985fa48ebe43486832
def git_rev():
    """
    Get the git revision for the repo in the path *repo*.
    Returns the commit id of the current head.
    Note: this function parses the files in the git repository directory
    without using the git application.  It may break if the structure of
    the git repository changes.  It only reads files, so it should not do
    any damage to the repository in the process.
    """
    # Based on stackoverflow am9417
    # https://stackoverflow.com/questions/14989858/get-the-current-git-hash-in-a-python-script/59950703#59950703
    git_root = BASE_MATTERPORTDL_DIR / ".git"
    git_head = git_root / "HEAD"
    if not git_head.exists():
        return None

    # Read .git/HEAD file
    with git_head.open("r") as fd:
        head_ref = fd.read()

    # Find head file .git/HEAD (e.g. ref: ref/heads/master => .git/ref/heads/master)
    if not head_ref.startswith("ref: "):
        return head_ref
    head_ref = head_ref[5:].strip()

    # Read commit id from head file
    head_path = git_root.joinpath(*head_ref.split("/"))
    if not head_path.exists():
        return None

    with head_path.open("r") as fd:
        commit = fd.read().strip()

    return f"{head_ref} ({commit})"


def sys_info():
    str = "Running " + SCRIPT_NAME + " with python "
    try:
        str += platform.python_version()
        str += " on " + sys.platform
        ourVersion = None
        try:
            ourVersion = git_rev()
        except Exception:
            pass

        if ourVersion is None:
            ourVersion = "S " + self_sha()

        str += " with matterport-dl version: " + ourVersion
    except Exception:
        pass
    return str


def makeDirs(dirname):
    global dirsMadeCache
    if dirname in dirsMadeCache:
        return
    pathlib.Path(dirname).mkdir(parents=True, exist_ok=True)
    dirsMadeCache[dirname] = True


def consoleDebugLog(msg: str, loglevel=logging.INFO, forceDebugOn=False):
    logging.log(loglevel, msg)
    if not CLA.getCommandLineArg(CommandLineArg.CONSOLE_LOG) and (forceDebugOn or CLA.getCommandLineArg(CommandLineArg.DEBUG)):
        print(msg)


def consoleLog(msg: str, loglevel=logging.INFO):
    consoleDebugLog(msg, loglevel, True)


def getModifiedName(filename: str):
    filename, _, query = filename.partition("?")
    basename = filename
    ext = ""
    pos = filename.rfind(".")
    if pos != -1:
        ext = basename[pos + 1 :]
        basename = basename[0:pos]
    if query:
        ext += "?" + query
    return f"{basename}.modified.{ext}"


def getVariants():
    variants = []
    global SWEEP_DO_4K
    # to be smart we should be using the GetShowcaseSweeps file and data.model.locations[4].pano.resolutions  to determine if we should be trying 2k or 4k
    depths = ["512", "1k", "2k"]
    if SWEEP_DO_4K:
        depths.append("4k")
    for depth in range(len(depths)):
        z = depths[depth]
        for x in range(2**depth):
            for y in range(2**depth):
                for face in range(6):
                    variants.append(f"{z}_face{face}_{x}_{y}.jpg")
    return variants


async def downloadDAM(accessurl, uuid):
    # This should have already been downloaded during the ADV download
    damSrcFile = f"..{os.path.sep}{uuid}_50k.dam"
    await downloadFile("UUID_DAM50K", True, accessurl.format(filename=f"{uuid}_50k.dam"), f"..{os.path.sep}{uuid}_50k.dam", key_type=AccessKeyType.FILES3_TEMPLATE_KEY)
    shutil.copy(damSrcFile, f"{uuid}_50k.dam")  # so the url here has the ~ in it but the primary dir is the parent sitl lwe will store it both places
    cur_file = ""
    try:
        for i in range(1000):  # basically download until on first failure and assume that is all of them, maybe we should be going to on first 404 or osmething:)
            cur_file = accessurl.format(filename=f"{uuid}_50k_texture_jpg_high/{uuid}_50k_{i:03d}.jpg")
            await downloadFile("UUID_TEXTURE_HIGH", True, cur_file, f"{uuid}_50k_texture_jpg_high/{uuid}_50k_{i:03d}.jpg")
            cur_file = accessurl.format(filename=f"{uuid}_50k_texture_jpg_low/{uuid}_50k_{i:03d}.jpg")
            await downloadFile("UUID_TEXTURE_LOW", True, cur_file, f"{uuid}_50k_texture_jpg_low/{uuid}_50k_{i:03d}.jpg")
    except Exception as ex:
        logging.warning(f"Exception downloading file: {cur_file} of: {str(ex)}")
        pass  # very lazy and bad way to only download required files


async def downloadSweeps(accessurl: str, sweeps: list[str]):
    global MODEL_IS_DEFURNISHED
    # the sweep query at least has data.model.defurnishViews[0].model.id for others
    forceKey = AccessKeyType.PrimaryKey
    if MODEL_IS_DEFURNISHED:
        forceKey = AccessKeyType.SWEEP_KEY
    toDownload: list[AsyncDownloadItem] = []
    for sweep in sweeps:
        sweep = sweep.replace("-", "")
        for variant in getVariants():  # so if we checked for 404s we could do this more effeciently but serializing it to do that would be slower than just a bunch of 404s
            toDownload.append(AsyncDownloadItem("MODEL_SWEEPS", True, accessurl.format(filename=f"tiles/{sweep}/{variant}") + "&imageopt=1", f"tiles/{sweep}/{variant}", key_type=forceKey))
    await AsyncArrayDownload(toDownload)


def expandTileUrlTemplate(template: str, tileCount: int) -> list[str]:
    urls = []
    for x in range(tileCount):
        for y in range(tileCount):
            for face in range(6):
                urls.append(template.replace("<face>", str(face)).replace("<x>", str(x)).replace("<y>", str(y)))
    return urls


def overlayTileGridSize(template: str, tileCount) -> int:
    # tileCount is the grid size per axis (1, 2, 4, 8), same progression getVariants uses
    if isinstance(tileCount, int) and tileCount > 0:
        return tileCount
    name = template.split("?", 1)[0].rsplit("/", 1)[-1]
    return {"512": 1, "1k": 2, "2k": 4, "4k": 8}.get(name.split("_face", 1)[0], 0)


_OVERLAY_TILE_TEMPLATE_RE = re.compile(r"https://cdn-\d+\.matterport(?:vr)?\.(?:com|cn)/models/[^\s\"']+/overlay/[^\s\"']+/~/tiles/[^\s\"']+_face<face>_<x>_<y>\.jpg\?t=[^\s\"'&]+&k=[^\s\"']+")


def overlayTemplatesFromShowPage(page_text: str) -> dict[str, int]:
    # The show page embeds the tile URLs with a signature that lasts about a day. The copy in
    # GetShowcaseSweeps is a different signature and often expires within minutes.
    if not page_text:
        return {}
    decoded = page_text.replace("\\u003C", "<").replace("\\u003E", ">").replace("\\u003D", "=").replace("\\u0026", "&").replace("\\/", "/")
    templates: dict[str, int] = {}
    for url in _OVERLAY_TILE_TEMPLATE_RE.findall(decoded):
        grid = overlayTileGridSize(url, None)
        if grid > 0:
            templates.setdefault(url, grid)
    return templates


def overlayTemplatesFromGraph() -> tuple[dict[str, int], set[str]]:
    graph_path = "api/mp/models/graph_GetShowcaseSweeps.json"
    if not os.path.exists(graph_path):
        return {}, set()
    try:
        with open(graph_path, "r", encoding="UTF-8") as f:
            graph = json.load(f)
        locations = graph["data"]["model"]["locations"]
    except Exception:
        consoleLog("GetShowcaseSweeps has no sweep locations (often an APQ cache miss)", logging.WARNING)
        return {}, set()
    if not isinstance(locations, list):
        return {}, set()
    templates: dict[str, int] = {}
    locked_paths: set[str] = set()
    for location in locations:
        for skybox in location.get("pano", {}).get("skyboxes", []):
            template = skybox.get("tileUrlTemplate") or ""
            if "/overlay/" not in template or "/tiles/" not in template or "<face>" not in template:
                continue
            if skybox.get("status") == "locked":
                locked_paths.add(urlparse(template).path)
                continue
            grid = overlayTileGridSize(template, skybox.get("tileCount"))
            if grid > 0:
                templates.setdefault(template, grid)
    return templates, locked_paths


async def downloadOverlayTiles(show_page_text: str = ""):
    global THIS_MODEL_ROOT_DIR, MODEL_IS_DEFURNISHED
    # The main-model access key is rejected (403) for overlay cube-face tiles. Use the signed
    # URLs from the show page and leave t= and k= unchanged.
    if MODEL_IS_DEFURNISHED:
        return
    show_templates = overlayTemplatesFromShowPage(show_page_text)
    graph_templates, locked_paths = overlayTemplatesFromGraph()
    if show_templates:
        templates = {url: grid for url, grid in show_templates.items() if urlparse(url).path not in locked_paths}
        source = "show page"
    else:
        templates = graph_templates
        source = "GetShowcaseSweeps"
        if templates:
            consoleLog("Show page has no overlay tile URLs; using GetShowcaseSweeps. Those signatures often expire within minutes.", logging.WARNING)
    locked = len(locked_paths)

    if not templates:
        if locked:
            consoleLog(f"Overlay face tiles are locked ({locked} templates), skipping", logging.WARNING)
        else:
            consoleLog("No overlay face tiles found in the show page or GetShowcaseSweeps")
        return

    os.chdir(THIS_MODEL_ROOT_DIR)
    toDownload: list[AsyncDownloadItem] = []
    for template, grid in templates.items():
        for url in expandTileUrlTemplate(template, grid):
            toDownload.append(AsyncDownloadItem("OVERLAY_TILES", True, url, urlparse(url).path[1:], key_type=AccessKeyType.LeaveKeyAlone))
    consoleLog(f"Downloading {len(toDownload)} overlay face tiles from {len(templates)} {source} templates ({locked} locked skipped)...")
    await AsyncArrayDownload(toDownload)
    consoleLog(f"Done with overlay tiles ({len(toDownload)} tile requests).")


# these 3 downwload with json posts were old functions for old graphql queries we dont need/use ducrrently
async def downloadFileWithJSONPostAndGetText(type, shouldExist, url, file, post_json_str, descriptor, always_download=False):
    if not CLA.getCommandLineArg(CommandLineArg.TILDE):
        file = file.replace("~", "_")

    await downloadFileWithJSONPost(type, shouldExist, url, file, post_json_str, descriptor, always_download)
    if not os.path.exists(file):
        return ""
    else:
        async with aiofiles.open(file, "r", encoding="UTF-8") as f:
            return await f.read()


# does not use access keys currently not needed
async def downloadFileWithJSONPost(type, shouldExist, url, file, post_json_str, descriptor, always_download=False):
    global OUR_SESSION
    if not CLA.getCommandLineArg(CommandLineArg.TILDE):
        file = file.replace("~", "_")
    if "/" in file:
        makeDirs(os.path.dirname(file))

    if not CLA.getCommandLineArg(CommandLineArg.DOWNLOAD) or (os.path.exists(file) and not always_download):  # skip already downloaded files except forced downloads
        logUrlDownloadSkipped(type, file, url, descriptor)
        return

    reqId = logUrlDownloadStart(type, file, url, descriptor, shouldExist, key_type=AccessKeyType.PrimaryKey)
    try:
        resp: requests.Response = await OUR_SESSION.request(url=url, method="POST", headers={"Content-Type": "application/json"}, data=bytes(post_json_str, "utf-8"))
        resp.raise_for_status()
        # req.add_header('Content-Length', len(body_bytes))
        async with aiofiles.open(file, "wb") as the_file:
            await the_file.write(resp.content)
        logUrlDownloadFinish(type, file, url, descriptor, shouldExist, reqId)
    except Exception as ex:
        logUrlDownloadFinish(type, file, url, descriptor, shouldExist, reqId, ex)
        raise Exception(f"Request error for url: {url} ({type}) that would output to: {file} of: {ex}") from ex  # str(ex) only is really doing the message not the entire cert  but we want the general error msg


async def GetTextOnlyRequest(type, shouldExist, url, post_data=None) -> str:
    global PROGRESS
    useTmpFileName = ""
    async with aiofiles.tempfile.NamedTemporaryFile(delete_on_close=False) as tmpFile:  # type: ignore
        useTmpFileName = cast(str, tmpFile.name)

    result = await downloadFileAndGetText(type, shouldExist, url, useTmpFileName, post_data)
    PROGRESS.Increment(ProgressType.Request, -1)
    PROGRESS.Increment(ProgressType.Success, -1)
    try:
        os.remove(useTmpFileName)
    except:
        pass
    return result


async def downloadFileAndGetText(type, shouldExist, url, file, post_data=None, isBinary=False, always_download=False, key_type: AccessKeyType = AccessKeyType.PrimaryKey):
    if not CLA.getCommandLineArg(CommandLineArg.TILDE):
        file = file.replace("~", "_")

    await downloadFile(type, shouldExist, url, file, post_data, always_download, key_type)
    if not os.path.exists(file):
        return ""
    else:
        readMode = "r"
        encoding = "UTF-8"
        if isBinary:
            readMode = "rb"
            encoding = None
        async with aiofiles.open(file, readMode, encoding=encoding) as f:  # type: ignore - r and rb are handled but by diff overload groups
            return await f.read()


def downloadTargetPath(file: str) -> str:
    if not CLA.getCommandLineArg(CommandLineArg.TILDE):
        file = file.replace("~", "_")
    if "?" in file:
        file = file.split("?", 1)[0]
    return file


# Add type parameter, shortResourcePath, shouldExist
async def downloadFile(type, shouldExist, url, file, post_data=None, always_download=False, key_type: AccessKeyType = AccessKeyType.PrimaryKey):
    global MAX_TASKS_SEMAPHORE, OUR_SESSION
    async with MAX_TASKS_SEMAPHORE:
        if key_type != AccessKeyType.LeaveKeyAlone:
            if key_type is None or key_type == AccessKeyType.PrimaryKey:
                key = KeyHandler.PrimaryKey
            else:
                key = KeyHandler.GetAccessKey(key_type)
            url = KeyHandler.SetAccessKeyForUrl(url, key)

        file = downloadTargetPath(file)

        if "/" in file:
            makeDirs(os.path.dirname(file))

        if not CLA.getCommandLineArg(CommandLineArg.DOWNLOAD) or (os.path.exists(file) and not always_download):  # skip already downloaded files except always download ones which are genreally ones that may contain keys?
            logUrlDownloadSkipped(type, file, url, "")
            return

        async def fetchAndSave():
            response = await OUR_SESSION.get(url)
            response.raise_for_status()  # Raise an exception if the response has an error status code
            async with aiofiles.open(file, "wb") as f:
                await f.write(response.content)

        reqId = logUrlDownloadStart(type, file, url, "", shouldExist, key_type=key_type)
        try:
            await fetchAndSave()
            logUrlDownloadFinish(type, file, url, "", shouldExist, reqId)
            return
        except Exception as err:
            # Matterport answers 429 when a burst is too fast. Wait 2, 4, 8, 16, then 32 seconds.
            if "Error 429" in f"{err}":
                backoff = INITIAL_429_BACKOFF
                for attempt in range(MAX_429_RETRIES):
                    consoleLog(f"Rate limited (429), retrying {file} in {backoff:.0f}s (attempt {attempt + 1}/{MAX_429_RETRIES})")
                    await asyncio.sleep(backoff)
                    try:
                        await fetchAndSave()
                        logUrlDownloadFinish(type, file, url, "", shouldExist, reqId)
                        return
                    except Exception as retry_err:
                        err = retry_err
                        if "Error 429" not in f"{retry_err}":
                            break
                        backoff *= 2

            # Try again but with different accesskeys, if error is 404 though no need to retry
            if "?t=" in url and "Error 404" not in f"{err}":
                if False:  # disable brute forcing at a minimum probably shouldnt do getallkeys just primary
                    for key in KeyHandler.GetAllKeys():
                        url2 = ""
                        try:
                            url2 = KeyHandler.SetAccessKeyForUrl(url, key)
                            response = await OUR_SESSION.get(url2)
                            response.raise_for_status()  # Raise an exception if the response has an error status code

                            async with aiofiles.open(file, "wb") as f:
                                await f.write(response.content)
                            logUrlDownloadFinish(type, file, url2, "", shouldExist, reqId)
                            return
                        except Exception as err2:
                            logUrlDownloadFinish(type, file, url2, "", shouldExist, reqId, err2, True)
                            pass
            logUrlDownloadFinish(type, file, url, "", shouldExist, reqId, err)
            raise Exception(f"Request error for url: {url} ({type}) that would output to: {file} of: {err}") from err


MAX_429_RETRIES = 5  # retries after the first HTTP 429
INITIAL_429_BACKOFF = 2.0  # seconds; doubles each retry (2, 4, 8, 16, 32)


def validUntilFix(text):
    # Plain JSON ("validUntil": "2026-...") and the escaped form baked into the show page (\"validUntil\":\"2026-...)
    text = re.sub(r"validUntil\"\s*:\s*\"20[\d]{2}-[\d]{2}-[\d]{2}T", 'validUntil":"2099-01-01T', text)
    text = re.sub(r'validUntil\\"\s*:\s*\\"20[\d]{2}-[\d]{2}-[\d]{2}T', 'validUntil\\":\\"2099-01-01T', text)
    return text


async def downloadGraphModels(pageid):
    global GRAPH_DATA_REQ, BASE_MATTERPORT_DOMAIN, MODEL_IS_DEFURNISHED
    makeDirs("api/mp/models")

    for key in GRAPH_DATA_REQ:
        file_path_base = f"api/mp/models/graph_{key}"
        file_path = f"{file_path_base}.json"
        req_url = GRAPH_DATA_REQ[key].replace("[MATTERPORT_MODEL_ID]", pageid)
        text = await downloadFileAndGetText("GRAPH_MODEL", True, f"https://my.{BASE_MATTERPORT_DOMAIN}/api/mp/models/graph{req_url}", file_path, always_download=CLA.getCommandLineArg(CommandLineArg.REFRESH_KEY_FILES) and CLA.getCommandLineArg(CommandLineArg.ALWAYS_DOWNLOAD_GRAPH_REQS))
        KeyHandler.SaveKeysFromText(f"GRAPH_{key}", text)
        if key == "GetModelViewPrefetch" and not MODEL_IS_DEFURNISHED:
            prefetchKeys = KeyHandler.GetKeysFromStr(text)
            if len(prefetchKeys) > 0:
                KeyHandler.SetAccessKey(AccessKeyType.GRAPH_MODEL_VIEW_PREFETCH, prefetchKeys[0])

        # Patch (graph_GetModelDetails.json & graph_GetSnapshots.json and such) URLs to Get files form local server instead of https://cdn-2.matterport.com/
        if CLA.getCommandLineArg(CommandLineArg.MANUAL_HOST_REPLACEMENT):
            text = text.replace(f"https://cdn-2.{BASE_MATTERPORT_DOMAIN}", "http://127.0.0.1:8080")  # without the localhost it seems like it may try to do diff
            text = text.replace(f"https://cdn-1.{BASE_MATTERPORT_DOMAIN}", "http://127.0.0.1:8080")  # without the localhost it seems like it may try to do diff
        text = validUntilFix(text)

        async with aiofiles.open(getModifiedName(file_path), "w", encoding="UTF-8") as f:
            await f.write(text)


ProgressType = Enum("ProgressType", ["Request", "Success", "Skipped", "Failed404", "Failed403", "FailedUnknown"])


class ProgressStats:
    def __str__(self):
        relInfo = ""
        if self.relativeTo is not None:
            relInfo = "Relative "
        return f"{relInfo}Total fetches: {self.TotalPosRequests()} {self.ValStr(ProgressType.Skipped)} actual {self.ValStr(ProgressType.Request)} {self.ValStr(ProgressType.Success)} {self.ValStr(ProgressType.Failed403)} {self.ValStr(ProgressType.Failed404)} {self.ValStr(ProgressType.FailedUnknown)}"

    def RelativeMark(self):
        self.relativeTo = dict(self.stats)

    def ClearRelative(self):
        self.relativeTo = None

    relativeTo: dict[ProgressType, int] | None

    def __init__(self):
        self.stats: dict[ProgressType, int] = dict()
        # self.locks : dict[ProgressType,asyncio.Semaphore] = dict()
        self.locks: dict[ProgressType, threading.Lock] = dict()
        for typ in ProgressType:
            self.stats[typ] = 0
            self.locks[typ] = threading.Lock()

    def Val(self, typ: ProgressType):
        val = self.stats[typ]
        if self.relativeTo is not None:
            val -= self.relativeTo[typ]
        return val

    def TotalPosRequests(self):
        ret = self.Val(ProgressType.Request) + self.Val(ProgressType.Skipped)
        if ret == 0:
            ret = 0.01  # avoid div by 0
        return ret

    def ValStr(self, typ: ProgressType):
        val = self.Val(typ)
        perc = f" ({val / self.TotalPosRequests():.0%})"
        return f"{typ.name}: {self.Val(typ)}{perc}"

    def Increment(self, typ: ProgressType, amt: int = 1):
        with self.locks[typ]:
            self.stats[typ] += amt
            return self.stats[typ]


PROGRESS = ProgressStats()


def logUrlDownloadFinish(type, localTarget, url, additionalParams, shouldExist, requestID, error=None, altUrlExists=False):
    global PROGRESS
    logLevel = logging.INFO
    prefix = "Finished"
    if error:
        if altUrlExists:
            logLevel = logging.WARNING
            error = f"PartErr of: {error}"
            prefix = "aTryFail"
        else:
            logLevel = logging.ERROR
            error = f"Error of: {error}"
            prefix = "aFailure"
            PROGRESS.Increment(ProgressType.Failed403 if "Error 403" in error else ProgressType.Failed404 if "Error 404" in error else ProgressType.FailedUnknown)
    else:
        PROGRESS.Increment(ProgressType.Success)
        error = ""
    _logUrlDownload(logLevel, prefix, type, localTarget, url, additionalParams, shouldExist, requestID, error)  # not sure if should lower log elve for shouldExist  false


def logUrlDownloadSkipped(type, localTarget, url, additionalParams):
    global PROGRESS
    PROGRESS.Increment(ProgressType.Skipped)
    _logUrlDownload(logging.DEBUG, "Skipped already downloaded", type, localTarget, url, additionalParams, False, "")


def logUrlDownloadStart(type, localTarget, url, additionalParams, shouldExist, key_type):
    global PROGRESS
    ourReqId = PROGRESS.Increment(ProgressType.Request)
    _logUrlDownload(logging.DEBUG, "Starting", type, localTarget, url, additionalParams, shouldExist, ourReqId, key_type=key_type)
    return ourReqId


def _logUrlDownload(logLevel, logPrefix, type, localTarget, url, additionalParams, shouldExist, requestID, optionalResult=None, key_type=AccessKeyType.PrimaryKey):
    global CLA
    if not CLA.getCommandLineArg(CommandLineArg.DOWNLOAD):
        return
    if optionalResult:
        optionalResult = f"Result: {optionalResult}"
    else:
        optionalResult = ""
    if key_type is not None:
        key_type = f" KeyType: {key_type}"
    else:
        key_type = ""
    logging.log(logLevel, f"{logPrefix} REQ for {type} {requestID}: should exist: {shouldExist} {optionalResult} File: {localTarget} at url: {url} {key_type} {additionalParams}")


def parseJSDictBody(forWhat: str, body: str):
    # Parses the inside of a JS object literal with numeric keys ie: 239:"three-examples",777:"split"
    ret: dict[str, str] = {}
    for kvp in body.split(","):
        arr = kvp.replace('"', "").split(":")
        if len(arr) < 2:
            continue
        try:
            key = int(float(arr[0]))  # keys can be in scientific notation
        except ValueError:
            consoleDebugLog(f"Skipping entry in JS dictionary for: {forWhat} that has a non numeric key of: {arr[0]}")
            continue
        ret[f"{key}"] = ":".join(arr[1:])
    return ret


def parseBalancedJSDict(text: str, bracePos: int):
    # Takes text and the index of a { character, returns the text between the braces and the index just after the matching }, ("", -1) if unbalanced
    depth = 0
    for i in range(bracePos, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[bracePos + 1 : i], i + 1
    return "", -1


def findAllBalancedJSDicts(text: str):
    # Returns the body (without the braces) of every top level {} in the text
    ret: list[str] = []
    pos = 0
    while True:
        bracePos = text.find("{", pos)
        if bracePos == -1:
            return ret
        body, endPos = parseBalancedJSDict(text, bracePos)
        if endPos == -1:
            return ret
        ret.append(body)
        pos = endPos


# The showcase runtime js builds the chunk file names from a couple of maps, the names map is optional and so is the key (hash) map, the formats seen so far are:
#   d.u = e => "js/" + ({239:"three-examples",9114:"core"}[e] || e) + "." + {172:"6c50ed8e5ff7620de75b",9114:"8aa28bbfc8f4948fd4d1"}[e] + ".js"  # names + keys
#   d.u = e => "js/" + ({239:"three-examples",9114:"core"}[e] || e) + ".js"  # names only, what the 2026 and newer builds do (no key map at all)
#   d.u = e => "js/" + e + "." + {172:"6c50ed8e5ff7620de75b"}[e] + ".js"  # keys only
# with the css one being d.miniCssF = e => "css/" + ({7475:"late",9114:"core"}[e] || e) + ".css" (the css key is always 1 so it is never in the file)
# Every css chunk id is listed in the mini css chunk map, ie: d.f.miniCss = (r,o) => { ... {1991:1,7475:1,9114:1}[r] && ... }
# returns (jsNames, jsKeys, cssNames, cssChunkIds), jsKeys is empty on the newer builds that have no key map
def extractWebpackChunkMaps(runtimeText: str):
    jsMatch = re.search(r'"js/"\+(?P<body>.+?)\+"\.js"', runtimeText, flags=re.S)
    if jsMatch is None:
        raise Exception('Unable to find the js chunk url function (the "js/"+ ... +".js" part) in the showcase runtime js file, matterport may have changed the format')
    jsBody = jsMatch.group("body")
    jsDicts = findAllBalancedJSDicts(jsBody)
    if '+"."+{' in jsBody:  # the key map is there so the chunk file names include a per chunk key (hash)
        jsNames = parseJSDictBody("showcase-runtime.js: namedJSFiles", jsDicts[0]) if len(jsDicts) >= 2 else {}
        jsKeys = parseJSDictBody("showcase-runtime.js: JSFileToKey", jsDicts[-1])
    else:
        jsNames = parseJSDictBody("showcase-runtime.js: namedJSFiles", jsDicts[0]) if len(jsDicts) >= 1 else {}
        jsKeys = {}

    cssNames: dict[str, str] = {}
    cssMatch = re.search(r'"css/"\+(?P<body>.+?)\+"\.css"', runtimeText, flags=re.S)
    if cssMatch is not None:
        cssDicts = findAllBalancedJSDicts(cssMatch.group("body"))
        if len(cssDicts) >= 1:
            cssNames = parseJSDictBody("showcase-runtime.js: namedCSSFiles", cssDicts[0])

    cssChunkIds: set[str] = set()
    cssMap = re.search(r"\{(?P<ids>(?:[0-9]{1,7}:1,)*[0-9]{1,7}:1)\}\[", runtimeText)
    if cssMap is not None:
        cssChunkIds = set(re.findall(r"[0-9]+(?=:1)", cssMap.group("ids")))
    if len(cssChunkIds) == 0:  # no map of every css chunk, the named ones are all we know about
        cssChunkIds = set(cssNames.keys())
    return jsNames, jsKeys, cssNames, cssChunkIds


async def downloadAssets(base, base_page_text):
    global PROGRESS, BASE_MATTERPORT_DOMAIN, MAIN_SHOWCASE_FILENAME

    language_codes = ["af", "sq", "ar-SA", "ar-IQ", "ar-EG", "ar-LY", "ar-DZ", "ar-MA", "ar-TN", "ar-OM", "ar-YE", "ar-SY", "ar-JO", "ar-LB", "ar-KW", "ar-AE", "ar-BH", "ar-QA", "eu", "bg", "be", "ca", "zh-TW", "zh-CN", "zh-HK", "zh-SG", "hr", "cs", "da", "nl", "nl-BE", "en", "en-US", "en-EG", "en-AU", "en-GB", "en-CA", "en-NZ", "en-IE", "en-ZA", "en-JM", "en-BZ", "en-TT", "et", "fo", "fa", "fi", "fr", "fr-BE", "fr-CA", "fr-CH", "fr-LU", "gd", "gd-IE", "de", "de-CH", "de-AT", "de-LU", "de-LI", "el", "he", "hi", "hu", "is", "id", "it", "it-CH", "ja", "ko", "lv", "lt", "mk", "mt", "no", "pl", "pt-BR", "pt", "rm", "ro", "ro-MO", "ru", "ru-MI", "sz", "sr", "sk", "sl", "sb", "es", "es-AR", "es-GT", "es-CR", "es-PA", "es-DO", "es-MX", "es-VE", "es-CO", "es-PE", "es-EC", "es-CL", "es-UY", "es-PY", "es-BO", "es-SV", "es-HN", "es-NI", "es-PR", "sx", "sv", "sv-FI", "th", "ts", "tn", "tr", "uk", "ur", "ve", "vi", "xh", "ji", "zu"]

    language_codes = ["zh-TW", "zh-CN", "nl", "de", "it", "ja", "ko", "pt", "ru", "es"]  # these are the only language codes that seem to succeed if a model works with one other than this please file a bug report and let us know, these are hardcoded into showcase.js file

    font_files = ["ibm-plex-sans-100", "ibm-plex-sans-100italic", "ibm-plex-sans-200", "ibm-plex-sans-200italic", "ibm-plex-sans-300", "ibm-plex-sans-300italic", "ibm-plex-sans-500", "ibm-plex-sans-500italic", "ibm-plex-sans-600", "ibm-plex-sans-600italic", "ibm-plex-sans-700", "ibm-plex-sans-700italic", "ibm-plex-sans-italic", "ibm-plex-sans-regular", "mp-font", "roboto-100", "roboto-100italic", "roboto-300", "roboto-300italic", "roboto-500", "roboto-500italic", "roboto-700", "roboto-700italic", "roboto-900", "roboto-900italic", "roboto-italic", "roboto-regular"]

    # extension assumed to be .png unless it is .svg or .jpg, for anything else place it in assets
    image_files = ["360_placement_pin_mask", "chrome", "Desktop-help-play-button.svg", "Desktop-help-spacebar", "edge", "escape", "exterior", "exterior_hover", "firefox", "interior", "interior_hover", "matterport-logo-light.svg", "matterport-logo.svg", "mattertag-disc-128-free.v1", "mobile-help-play-button.svg", "nav_help_360", "nav_help_click_inside", "nav_help_gesture_drag", "nav_help_gesture_drag_two_finger", "nav_help_gesture_pinch", "nav_help_gesture_position", "nav_help_gesture_position_two_finger", "nav_help_gesture_tap", "nav_help_inside_key", "nav_help_keyboard_all", "nav_help_keyboard_left_right", "nav_help_keyboard_up_down", "nav_help_mouse_click", "nav_help_mouse_ctrl_click", "nav_help_mouse_drag_left", "nav_help_mouse_drag_right", "nav_help_mouse_position_left", "nav_help_mouse_position_right", "nav_help_mouse_zoom", "nav_help_tap_inside", "nav_help_zoom_keys", "NoteColor", "pinAnchor", "safari", "scope.svg", "showcase-password-background.jpg", "surface_grid_planar_256", "vert_arrows", "headset-quest-2", "tagColor", "matterport-app-icon.svg"]

    assets = ["js/browser-check.js", "css/showcase.css", "css/packages-nova-ui.css", "css/scene.css", "css/unsupported_browser.css", "cursors/grab.png", "cursors/grabbing.png", "cursors/zoom-in.png", "cursors/zoom-out.png", "locale/strings.json", "css/ws-blur.css", "css/core.css", "css/late.css"]

    # following seem no more: "css/split.css", "headset-cardboard", "headset-quest", "NoteIcon",  "puck_256_red", "tagbg", "tagmask", "roboto-700-42_0", "pinIconDefault",

    # downloadFile("my.matterport.com/favicon.ico", "favicon.ico")
    base_page_js_loads = re.findall(r"script\s+(?:defer\s+)?src=[\"']([^\"']+[.]js)[\"']", base_page_text, flags=re.IGNORECASE)

    # now they use module imports as well like: import(importBase + 'js/runtime~showcase.69d7273003fd73b7a8f3.js'),

    import_js_loads = re.findall(r'import\([^\'\"()]*[\'"]([^\'"()]+\.js)[\'"]\s*\)', base_page_text, flags=re.IGNORECASE)

    for js in import_js_loads:
        base_page_js_loads.append(js)

    typeDict: dict[str, str] = {}
    for asset in assets:
        typeDict[asset] = "STATIC_ASSET"

    showcase_runtime_filename: str = None
    react_vendor_filename: str = None

    if CLA.getCommandLineArg(CommandLineArg.DEBUG):
        DebugSaveFile("js_found.txt", "\n".join(base_page_js_loads))

    for js in base_page_js_loads:
        file = js
        if "://" in js:
            consoleDebugLog(f"Skipping {js} should be the three.js file as the only non-relative one")
        # if "://" not in js:
        # file = base + js
        if file in assets:
            continue
        typeDict[file] = "HTML_DISCOVERED_JS"
        if "showcase" in js:
            if "runtime" in js:
                showcase_runtime_filename = file
            else:
                MAIN_SHOWCASE_FILENAME = file
                assets.append(file)

        else:
            if "vendors-react" in js:
                react_vendor_filename = file
            assets.append(file)

    if showcase_runtime_filename is None:
        raise Exception("In all js files found on the page could not find any that have showcase and runtime in the filename for the showcase runtime js file")
    if react_vendor_filename is None:
        raise Exception("In all js files found on the page could not find any that have vendors-react in the filename for the react vendor js file")
    await downloadFile("STATIC_ASSET", True, "https://matterport.com/nextjs-assets/images/favicon.ico", "favicon.ico")  # mainly to avoid the 404, always matterport.com
    showcase_cont = await downloadFileAndGetText(typeDict[showcase_runtime_filename], True, base + showcase_runtime_filename, showcase_runtime_filename, always_download=CLA.getCommandLineArg(CommandLineArg.REFRESH_KEY_FILES))

    # lets try to extract the js files it might be loading and make sure we know them, the code has things like .e(858) to load which are the numbers we care about
    # see extractWebpackChunkMaps for what the chunk url functions in the runtime js look like
    jsNamedDict, jsKeyDict, cssNamedDict, cssChunkIds = extractWebpackChunkMaps(showcase_cont)

    def chunkJsFile(chunkId: str):
        name = jsNamedDict.get(chunkId, chunkId)  # chunks with no name in the map use the chunk id as the file name
        if chunkId in jsKeyDict:
            return f"js/{name}.{jsKeyDict[chunkId]}.js"
        return f"js/{name}.js"

    # When there is a key map every js chunk is listed in it, when there isn't (newer builds) the chunk ids can only be
    # discovered from the chunk load calls in the js, which means waiting until after the js has been downloaded, see below
    jsChunkIds: set[str] = set(jsKeyDict.keys())

    for number in jsKeyDict.keys():
        file = chunkJsFile(number)
        typeDict[file] = "SHOWCASE_DISCOVERED_JS"
        assets.append(file)

    for number in cssChunkIds:
        name = cssNamedDict.get(number, number)
        file = f"css/{name}.css"  # key is not used for css its just 1 always
        typeDict[file] = "SHOWCASE_DISCOVERED_CSS"
        assets.append(file)

    for image in image_files:
        if not image.endswith(".jpg") and not image.endswith(".svg"):
            image = image + ".png"
        file = "images/" + image
        typeDict[file] = "STATIC_IMAGE"
        assets.append(file)

    for f in font_files:
        for file in ["fonts/" + f + ".woff", "fonts/" + f + ".woff2"]:
            typeDict[file] = "STATIC_FONT"
            assets.append(file)
    for lc in language_codes:
        file = "locale/messages/strings_" + lc + ".json"
        typeDict[file] = "STATIC_LOCAL_STRINGS"
        assets.append(file)

    toDownload: list[AsyncDownloadItem] = []
    for asset in assets:
        local_file = asset
        type = typeDict[asset]
        if local_file.endswith("/"):
            local_file = local_file + "index.html"
        shouldExist = True
        toDownload.append(AsyncDownloadItem(type, shouldExist, f"{base}{asset}", local_file))
    await AsyncArrayDownload(toDownload)
    if len(jsKeyDict) == 0:
        # Newer builds have no map listing every chunk so the chunk ids have to come from the chunk load calls and the
        # async import maps in the js. Chunks can load chunks that the main js never mentions so this goes in rounds
        # as more of the js arrives
        for roundNum in range(6):
            foundChunkIds: set[str] = set()
            for jsFile in pathlib.Path("js").glob("*.js"):
                try:
                    with open(jsFile, "r", encoding="utf-8", errors="ignore") as f:
                        foundChunkIds |= extractChunkIdsFromJs(f.read())
                except Exception as e:
                    consoleDebugLog(f"Failed to scan {jsFile} for chunk ids: {e}")
            newChunkIds = foundChunkIds - jsChunkIds
            if len(newChunkIds) == 0:
                break
            consoleDebugLog(f"Found {len(newChunkIds)} webpack chunks from chunk references, round: {roundNum}")
            jsChunkIds |= newChunkIds
            chunkDownloads: list[AsyncDownloadItem] = []
            for chunkId in sorted(newChunkIds):
                file = chunkJsFile(chunkId)
                chunkDownloads.append(AsyncDownloadItem("SHOWCASE_DISCOVERED_JS", False, f"{base}{file}", file))  # shouldExist false as some chunk ids are css only so have no js file
            await AsyncArrayDownload(chunkDownloads)

    # Same deal for static files the js/css references that we don't have a hardcoded entry for, ie: images/atlas.png
    referencedFiles: set[str] = set()
    for textFile in list(pathlib.Path("js").glob("*.js")) + list(pathlib.Path("css").glob("*.css")):
        try:
            with open(textFile, "r", encoding="utf-8", errors="ignore") as f:
                referencedFiles |= extractReferencedStaticFiles(f.read())
        except Exception as e:
            consoleDebugLog(f"Failed to scan {textFile} for referenced static files: {e}")
    referencedFiles = {f for f in referencedFiles if not os.path.exists(f) and f not in typeDict}
    if len(referencedFiles) > 0:
        consoleDebugLog(f"Found {len(referencedFiles)} static files referenced by the downloaded js/css")
        referencedDownloads: list[AsyncDownloadItem] = []
        for file in sorted(referencedFiles):
            referencedDownloads.append(AsyncDownloadItem("STATIC_DISCOVERED", False, f"{base}{file}", file))
        await AsyncArrayDownload(referencedDownloads)

    if react_vendor_filename and os.path.exists(react_vendor_filename):
        reactCont = ""
        with open(react_vendor_filename, "r", encoding="UTF-8") as f:
            reactCont = f.read()

        reactCont = reactCont.replace("(t.src=s.src)", '(t.src=""+(t.src??s.src))')  # hacky but in certain conditions react will try to reset the source on something after it loads to re-trigger the load event but this breaks jsnetproxy.  This allows the same triggering but uses the existing source if it exists. https://github.com/facebook/react/blob/37906d4dfbe80d71f312f7347bb9ddb930484d28/packages/react-dom-bindings/src/client/ReactFiberConfigDOM.js#L744. Right now this seems to only happens on embedded attachments.
        with open(getModifiedName(react_vendor_filename), "w", encoding="UTF-8") as f:
            f.write(reactCont)
    toDownload.clear()


async def downloadWebglVendors(base_page_text):
    regex = r"https://static.matterport.com/webgl-vendors/three/[a-z0-9\-_/.]*/three(?:\.[a-zA-Z0-9]+)?\.min\.js"
    threeMin = re.search(regex, base_page_text).group()  # type: ignore - may be None , this is always.com
    if threeMin is None:
        raise Exception(f"Unable to extract the 3d js file name from the page, regex did not match: {regex}")
    threeBase = threeMin.rpartition("/")[0]

    webglVendors = ["three.module.min.js", "three.core.min.js", "libs/draco/gltf/draco_wasm_wrapper.js", "libs/draco/gltf/draco_decoder.wasm", "libs/basis/basis_transcoder.wasm", "libs/basis/basis_transcoder.js"]
    toDownload: list[AsyncDownloadItem] = []

    for script in webglVendors:
        url = f"{threeBase}/{script}"
        toDownload.append(AsyncDownloadItem("WEBGL_FILE", False, url, urlparse(url).path[1:]))
    await AsyncArrayDownload(toDownload)


class AsyncDownloadItem:
    # shouldExist is purely for information in debugging, if false we are saying it might not and thats OK,  does not change any internal logic.
    # key_type overrides the accessKey we will change in the url,  set to AccessKeyType.LeaveKeyAlone to not change the one in the url
    def __init__(self, type: str, shouldExist: bool, url: str, file: str, key_type: AccessKeyType = AccessKeyType.PrimaryKey):
        self.type = type
        self.shouldExist = shouldExist
        self.url = url
        self.file = file
        self.key_type = key_type


class ExceptionWhatExceptionTaskGroup(asyncio.TaskGroup):
    def __init__(self):
        super().__init__()
        self._parent_cancel_requested = True  # hacky but required to prevent an aborted task from stopping us from being async

    def _abort(self):  # normally it goes through and cancels all the others now
        return None

    async def __aexit__(self, et, exc, tb):  # at end of block it would throw any exceptions
        try:
            await super().__aexit__(et, exc, tb)
        except:
            pass


async def AsyncArrayDownload(assets: list[AsyncDownloadItem]):
    PROGRESS.RelativeMark()
    pending: list[AsyncDownloadItem] = []
    already = 0
    for asset in assets:
        # Checking here avoids a task and a progress tick per file that is already saved.
        # A re-run otherwise spends minutes walking tens of thousands of existing tiles.
        if os.path.exists(downloadTargetPath(asset.file)):
            already += 1
        else:
            pending.append(asset)
    if already:
        PROGRESS.Increment(ProgressType.Skipped, already)
        consoleLog(f"Skipping {already} files already on disk")
    if not pending:
        return
    async with ExceptionWhatExceptionTaskGroup() as tg:
        for asset in tqdm(pending):
            tg.create_task(downloadFile(asset.type, asset.shouldExist, asset.url, asset.file, key_type=asset.key_type))
            await asyncio.sleep(0.001)  # we need some sleep or we will not yield
            while MAX_TASKS_SEMAPHORE.locked():
                await asyncio.sleep(0.01)
        logging.debug(f"{PROGRESS}")


# can get called twice for defurnished with the second call being the base model id
async def downloadFixedAPIInfo(pageid):
    global BASE_MATTERPORT_DOMAIN
    assets = [f"api/v1/jsonstore/model/highlights/{pageid}", f"api/v1/jsonstore/model/Labels/{pageid}", f"api/v1/jsonstore/model/mattertags/{pageid}", f"api/v1/jsonstore/model/measurements/{pageid}", f"api/v1/player/models/{pageid}/thumb?width=1707&dpr=1.5&disable=upscale", f"api/v2/models/{pageid}/sweeps", "api/v2/users/current", f"api/player/models/{pageid}/files", f"api/v1/jsonstore/model/trims/{pageid}", "api/v1/plugins?manifest=true", f"api/v1/jsonstore/model/plugins/{pageid}"]
    toDownload: list[AsyncDownloadItem] = []
    for asset in assets:
        local_file = asset
        if local_file.endswith("/"):
            local_file = local_file + "index.html"
        toDownload.append(AsyncDownloadItem("MODEL_INFO", True, f"https://my.{BASE_MATTERPORT_DOMAIN}/{asset}", local_file))
    await AsyncArrayDownload(toDownload)


async def downloadInfo(pageid):
    global BASE_MATTERPORT_DOMAIN, MODEL_IS_DEFURNISHED, BASE_MODEL_ID
    await downloadFixedAPIInfo(pageid)

    makeDirs("api/mp/models")
    with open("api/mp/models/graph", "w", encoding="UTF-8") as f:
        f.write('{"data": "empty"}')
    if MODEL_IS_DEFURNISHED:
        return

    pageJsonFile = f"api/v1/player/models/{pageid}/"
    modelJson = await downloadFileAndGetText("MODEL_INFO", True, f"https://my.{BASE_MATTERPORT_DOMAIN}/{pageJsonFile}", f"{pageJsonFile}/index.html", always_download=CLA.getCommandLineArg(CommandLineArg.REFRESH_KEY_FILES))
    KeyHandler.SaveKeysFromText("ApiV1PlayerModelsJson", modelJson)
    for i in range(1, 4):  # file to url mapping
        fileText = await downloadFileAndGetText("FILE_TO_URL_JSON", True, f"https://my.{BASE_MATTERPORT_DOMAIN}/api/player/models/{pageid}/files?type={i}", f"api/player/models/{pageid}/files_type{i}", always_download=CLA.getCommandLineArg(CommandLineArg.REFRESH_KEY_FILES))  # may have keys
        KeyHandler.SaveKeysFromText(f"FilesType{i}", fileText)  # used to be more elegant but now we can just gobble all the keys


# Chunk ids in the js come from two places, the chunk load calls (`.e(<chunkId>)`) and the async import maps which
# look like {"./cwf_en-US.yaml":[42622,2622],"./matterport-logo-white-r.svg":[58249,8249]}, the first number there is
# the module id and any after it are the chunks that have to be loaded for it
def extractChunkIdsFromJs(text: str):
    ids = set(re.findall(r"\.e\(([0-9]{1,7})\)", text))
    # A repeated capturing group only keeps its last match, so split the list and drop the module id.
    for m in re.finditer(r'"\./[^"]{1,200}":\[([0-9]{1,7}(?:,[0-9]{1,7})+)\]', text):
        ids.update(m.group(1).split(",")[1:])
    return ids


# The js/css can reference static files that aren't in the hardcoded lists above (images/atlas.png for example), some
# of what is found will be names built up at runtime that don't exist on the server, those are just a 404 in the log
def extractReferencedStaticFiles(text: str):
    ret: set[str] = set()
    for ref in re.findall(r"((?:images|cursors|fonts)/[A-Za-z0-9._\-/]+\.[A-Za-z0-9]{1,5})", text):
        ret.add(ref.split("?")[0])
    return ret


async def downloadPlugins(pageid):
    global BASE_MATTERPORT_DOMAIN
    pluginJson: Any
    with open("api/v1/plugins", "r", encoding="UTF-8") as f:
        pluginJson = json.loads(f.read())
    for plugin in pluginJson:
        versions = [plugin["currentVersion"]]
        match = re.match(r"(?P<base>[0-9.]+)-(?P<build>[0-9]+)$", plugin["currentVersion"])
        if match:  # the player strips a trailing build number when making the url, ie compass 1.0.14-1 asks for 1.0.14 (but color-grade 0.9.3-beta.5 asks for exactly that)
            versions.append(match.group("base"))
        for version in versions:
            plugPath = f"showcase-sdk/plugins/published/{plugin['name']}/{version}/plugin.json"
            try:
                await downloadFile("PLUGIN", version == versions[0], f"https://static.{BASE_MATTERPORT_DOMAIN}/{plugPath}", plugPath)
            except Exception as e:
                if version == versions[0]:
                    raise
                consoleDebugLog(f"Skipping plugin url that doesn't seem to exist: {plugPath} of: {e}")  # only the current version is guaranteed to be there


async def downloadAttachments():
    # May be the only thing from the actual prefetch graph we need;)
    try:
        with open("api/mp/models/graph_GetModelViewPrefetch.json", "r", encoding="UTF-8") as f:
            graphModelSnapshotsJson = json.loads(f.read())
        toDownload: list[AsyncDownloadItem] = []

        for mattertag in graphModelSnapshotsJson["data"]["model"]["mattertags"]:
            if "fileAttachments" in mattertag:
                for attachment in mattertag["fileAttachments"]:
                    toDownload.append(AsyncDownloadItem("MODEL_ATTACHMENTS", True, attachment["url"], urlparse(attachment["url"]).path[1:], key_type=AccessKeyType.LeaveKeyAlone))
        await AsyncArrayDownload(toDownload)

    except Exception:
        logging.exception("Unable to open graph model for prefetch output and download the embedded attachments...")
        return


async def downloadPics(pageid):
    # All these should already be downloaded through AdvancedAssetDownload likely they wont work here without a different access key any more....
    with open(f"api/v1/player/models/{pageid}/index.html", "r", encoding="UTF-8") as f:
        modeldata = json.load(f)
    toDownload: list[AsyncDownloadItem] = []
    for image in modeldata["images"]:
        toDownload.append(AsyncDownloadItem("MODEL_IMAGES", True, image["src"], urlparse(image["src"]).path[1:]))  # want want to use signed_src or download_url?
    await AsyncArrayDownload(toDownload)


async def downloadMainAssets(pageid, accessurl):
    global THIS_MODEL_ROOT_DIR, MODEL_IS_DEFURNISHED
    sweepUUIDs: list[str] = []
    if MODEL_IS_DEFURNISHED:  # technically we could use this for all, and this data is in the prefetch embedded as well
        with open("api/mp/models/graph_GetShowcaseSweeps.json", "r", encoding="UTF-8") as f:
            graphModelSweepsJson = json.loads(f.read())
            base_node = graphModelSweepsJson["data"]["model"]
            for location in base_node["locations"]:
                sweepUUIDs.append(location["pano"]["sweepUuid"])
            accessurl = base_node["locations"][0]["pano"]["skyboxes"][0]["tileUrlTemplate"]
            tildeStart = accessurl.find("~/")
            accessurl = accessurl[: tildeStart + 2]
            sweepDir = urlparse(accessurl).path[1:]
            if not CLA.getCommandLineArg(CommandLineArg.TILDE):
                sweepDir = sweepDir.replace("~", "_")
            accessurl = accessurl + "{filename}?t=2-796d5d010d7183bce7f0999701973d8b05b2df8f-1735673498-0"  # access key here doesnt matter as we will be replacing it
            makeDirs(sweepDir)
            os.chdir(sweepDir)
    else:
        # this uses the old model json but we dont need it, the dam should have already been downloaded and the sweeps we can use getShowcaseSweeeps for
        with open(f"api/v1/player/models/{pageid}/index.html", "r", encoding="UTF-8") as f:
            modeldata = json.load(f)
        match = re.search(r"models/([a-z0-9-_./~]*)/\{filename\}", accessurl)
        if match is None:
            raise Exception(f"Unable to extract access model id from url: {accessurl}")
        accessid = match.group(1)
        basePath = f"models/{accessid}"
        if not CLA.getCommandLineArg(CommandLineArg.TILDE):
            basePath = basePath.replace("~", "_")
        makeDirs(basePath)
        os.chdir(basePath)
        await downloadDAM(accessurl, modeldata["job"]["uuid"])
        sweepUUIDs = modeldata["sweeps"]
    # now: getShowcaseSweeps then need to iterate the locatiosn and get the uuid data.model.locations[0].pano.sweepUuid  this would resolve many of the 404s we will get by just bruteforcing  each location has its only max res (2k 4k etc)
    await downloadSweeps(accessurl, sweepUUIDs)  # sweeps are generally the biggest thing minus a few modles that have massive 3d detail items
    os.chdir(THIS_MODEL_ROOT_DIR)


# Patch showcase.js to fix expiration issue
def patchShowcase():
    global BASE_MATTERPORT_DOMAIN
    with open(MAIN_SHOWCASE_FILENAME, "r", encoding="UTF-8") as f:
        j = f.read()
    j = re.sub(r"\&\&\(!e.expires\|\|.{1,10}\*e.expires>Date.now\(\)\)", "", j)  # old
    j = j.replace("this.urlContainer.expires", "Date.now()")  # newer
    j = j.replace("this.onStale", "this.onStal")  # even newer
    if CLA.getCommandLineArg(CommandLineArg.MANUAL_HOST_REPLACEMENT):
        j = j.replace('"/api/mp/', '`${window.location.pathname}`+"api/mp/')
        j = j.replace("${this.baseUrl}", "${window.location.origin}${window.location.pathname}")

    j = j.replace(f'e.get("https://static.{BASE_MATTERPORT_DOMAIN}/geoip/",{{responseType:"json",priority:n.ru.LOW}})', '{"country_code":"US","country_name":"united states","region":"CA","city":"los angeles"}')
    if CLA.getCommandLineArg(CommandLineArg.MANUAL_HOST_REPLACEMENT):
        j = j.replace(f"https://static.{BASE_MATTERPORT_DOMAIN}", "")
    with open(getModifiedName(MAIN_SHOWCASE_FILENAME), "w", encoding="UTF-8") as f:
        f.write(j)


#    j = j.replace('"POST"','"GET"') #no post requests for external hosted
#    with open("js/showcase.js","w",encoding="UTF-8") as f:
#        f.write(j)


def drange(x, y, jump):
    while x < y:
        yield float(x)
        x += decimal.Decimal(jump)


def DebugSaveFile(fileName, fileContent):
    consoleLog(f"Saved debug file: {fileName}")
    with open(f"debug/{fileName}", "w") as the_file:
        the_file.write(fileContent)


def RemoteDomainsReplace(str: str):
    global BASE_MATTERPORT_DOMAIN
    domReplace = [f"static.{BASE_MATTERPORT_DOMAIN}", f"cdn-2.{BASE_MATTERPORT_DOMAIN}", f"cdn-1.{BASE_MATTERPORT_DOMAIN}", "mp-app-prod.global.ssl.fastly.net", f"events.{BASE_MATTERPORT_DOMAIN}"]

    # str = str.replace('"https://static.matterport.com','`${window.location.origin}${window.location.pathname}` + "').replace('"https://cdn-2.matterport.com','`${window.location.origin}${window.location.pathname}` + "').replace('"https://cdn-1.matterport.com','`${window.location.origin}${window.location.pathname}` + "').replace('"https://mp-app-prod.global.ssl.fastly.net/','`${window.location.origin}${window.location.pathname}` + "').replace('"https://events.matterport.com/', '`${window.location.origin}${window.location.pathname}` + "')
    if CLA.getCommandLineArg(CommandLineArg.MANUAL_HOST_REPLACEMENT):
        for dom in domReplace:
            str = str.replace(f"https://{dom}", "http://127.0.0.1:8080")

    return str


async def downloadCapture(pageid):
    global PROGRESS, RUN_ARGS_CONFIG_NAME, BASE_MATTERPORT_DOMAIN, CHINA_MATTERPORT_DOMAIN, THIS_MODEL_ROOT_DIR, MODEL_IS_DEFURNISHED, BASE_MODEL_ID
    makeDirs(pageid)
    BASE_MODEL_ID = pageid
    alias = CLA.getCommandLineArg(CommandLineArg.ALIAS)
    if alias and not os.path.exists(alias):
        os.symlink(pageid, alias)
    THIS_MODEL_ROOT_DIR = os.path.abspath(pageid)
    os.chdir(THIS_MODEL_ROOT_DIR)
    ROOT_FILE_COPY = ("JSNetProxy.js", "matterport-dl.py", "_matterport_interactive.py")
    for fl in ROOT_FILE_COPY:
        if not os.path.exists(fl):
            shutil.copy2(os.path.join(BASE_MATTERPORTDL_DIR, fl), fl)

    logging.basicConfig(filename="run_report.log", level=logging.DEBUG, format="%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S", encoding="utf-8", force=True)

    if CLA.getCommandLineArg(CommandLineArg.DEBUG):
        makeDirs("debug")
    if CLA.getCommandLineArg(CommandLineArg.CONSOLE_LOG):
        logging.getLogger().addHandler(logging.StreamHandler())
    consoleLog(f"Started up a download run {sys_info()}")

    url = f"https://my.{BASE_MATTERPORT_DOMAIN}/show/?m={pageid}"
    consoleLog(f"Downloading capture of {pageid} with base page... {url}")
    base_page_text = ""
    try:
        base_page_text: str = await downloadFileAndGetText("MAIN", True, url, "index.html", always_download=CLA.getCommandLineArg(CommandLineArg.REFRESH_KEY_FILES))

        curTitle = CLA.getCommandLineArg(CommandLineArg.TITLE)
        if curTitle == "":
            page_title = re.findall(r"<title>(.*)</title>", base_page_text)[0]
            page_title = page_title.rsplit("-", 1)[0].strip()
            CLA.setCommandLineArg(CommandLineArg.TITLE, page_title)

        CLA.SaveToFile(RUN_ARGS_CONFIG_NAME)

        if f"{CHINA_MATTERPORT_DOMAIN}/showcase" in base_page_text:
            BASE_MATTERPORT_DOMAIN = CHINA_MATTERPORT_DOMAIN
            consoleLog("Chinese matterport url found in main page, will try China server, note if this does not work try a proxy outside china")
        if CLA.getCommandLineArg(CommandLineArg.DEBUG):
            DebugSaveFile("base_page.html", base_page_text)  # noqa: E701

    except Exception as error:
        if "certificate verify failed" in str(error) or "SSL certificate problem" in str(error):
            raise TypeError(f"Error: {str(error)}. Have you tried running the Install Certificates.command (or similar) file in the python folder to install the normal root certs? If you are using a proxy without that cert installed in the python certificate library you may need to use: --no-verify-ssl") from error
        else:
            raise TypeError("First request error") from error

    KeyHandler.SaveKeysFromText("MainBasePage", base_page_text)
    staticbase = re.search(rf'<base href="(https://static.{BASE_MATTERPORT_DOMAIN}/.*?)">', base_page_text).group(1)  # type: ignore - may be None

    base_page_deunicode = base_page_text.encode("utf-8", errors="ignore").decode("unicode-escape")  # some non-english matterport pages have unicode escapes for even the generic url chars
    if CLA.getCommandLineArg(CommandLineArg.DEBUG):
        DebugSaveFile("base_page_deunicode.html", base_page_deunicode)  # noqa: E701
    match = re.search(r'"(?P<baseurl>https://cdn-\d*\.matterport(?:vr)?\.(?:com|cn)/models/[a-z0-9\-_/.]*/)(?:[{}0-9a-z_/<>.~]+)(?P<defaultAccessKey>\?t=.*?)"', base_page_deunicode)  # the ~/ optional is mostly for defurnished secondary models
    # matterportvr.cn
    if match:
        groupDict = match.groupdict()
        accessurl = f"{groupDict['baseurl']}~/{{filename}}{groupDict['defaultAccessKey']}"

    else:
        raise Exception(f"Can't find urls, try the main page: {url} in a browser to make sure it loads the model correctly")

    if not MODEL_IS_DEFURNISHED:
        # get a valid access key, there are a few but this is a common client used one, this also makes sure it is fresh
        file_type_content = await downloadFileAndGetText("MAIN", True, f"https://my.{BASE_MATTERPORT_DOMAIN}/api/player/models/{pageid}/files?type=3", f"api/player/models/{pageid}/files_type3", always_download=CLA.getCommandLineArg(CommandLineArg.REFRESH_KEY_FILES))  # get a valid access key, there are a few but this is a common client used one, this also makes sure it is fresh, note we would download this one later as well but we want this key early
        KeyHandler.SetAccessKey(AccessKeyType.FILES3_TEMPLATE_KEY, KeyHandler.GetKeysFromStr(file_type_content)[0])

    consoleLog("Downloading graph model data...")  # need the details one for advanced download
    await downloadGraphModels(pageid)

    # Automatic redirect if GET param isn't correct
    forcedProxyBase = "window.location.origin"
    # forcedProxyBase='"http://127.0.0.1:9000"'
    # window._ProxyAppendURL=1;
    injectedjs = 'if (!window.location.search.startsWith("?m=' + pageid + '")) { document.location.search = "?m=' + pageid + '"; };window._NoTilde=' + ("false" if CLA.getCommandLineArg(CommandLineArg.TILDE) else "true") + ";window._ProxyBase=" + forcedProxyBase + ";"
    content = base_page_text.replace(staticbase, ".")
    proxyAdd = ""
    if CLA.getCommandLineArg(CommandLineArg.MANUAL_HOST_REPLACEMENT):
        content = RemoteDomainsReplace(content)
    else:
        content = re.sub(r"(?P<preDomain>src\s*=\s*['" '"])https?://[^/"' "']+/", r"\g<preDomain>", content, flags=re.IGNORECASE)  # we replace any src= https://whatever.com  stripping the part up to the first slash
        content = re.sub(r"import\(\s*\s*(?P<quoteChar>['\"])https?://[^/\"']+/", r"import(\g<quoteChar>./", content, flags=re.IGNORECASE)  # similar to above but for import('http://...  must add ./ as well
        proxyAdd = "<script blocking='render' src='JSNetProxy.js'></script>"

    content = validUntilFix(content)
    content = content.replace("<head>", f"<head><script>{injectedjs}</script>{proxyAdd}")
    content = content.replace('from "https://static.matterport.com', 'from ".')  # fix the direct code import they added
    with open(getModifiedName("index.html"), "w", encoding="UTF-8") as f:
        f.write(content)

    consoleLog("Downloading model info...")
    await downloadInfo(pageid)
    urlKeyFind = CLA.getCommandLineArg(CommandLineArg.FIND_URL_KEY)
    urlKeyFindIsDownload = False
    if urlKeyFind == "":
        urlKeyFind = CLA.getCommandLineArg(CommandLineArg.FIND_URL_KEY_AND_DOWNLOAD)
    if CLA.getCommandLineArg(CommandLineArg.DEBUG):
        KeyHandler.DumpKnownKeysToFile()
        urlKeyFindIsDownload = True
    if urlKeyFind:
        await KeyHandler.PrintUrlKeys(urlKeyFind, urlKeyFindIsDownload)
        exit(0)

    consoleLog("Downloading Advanced Assets...")
    if CLA.getCommandLineArg(CommandLineArg.ADVANCED_DOWNLOAD):
        await AdvancedAssetDownload(base_page_text)

    consoleLog("Downloading static files...")
    await downloadAssets(staticbase, base_page_text)
    await downloadWebglVendors(base_page_text)
    # Patch showcase.js to fix expiration issue and some other changes for local hosting
    patchShowcase()
    consoleLog("Downloading plugins...")
    await downloadPlugins(pageid)
    if not MODEL_IS_DEFURNISHED:
        consoleLog("Downloading images...")
        await downloadPics(pageid)
    consoleLog("Downloading matterport tags / embedded attachments...")
    await downloadAttachments()
    open("api/v1/event", "a").close()
    if CLA.getCommandLineArg(CommandLineArg.MAIN_ASSET_DOWNLOAD):
        # Do this before the primary sweep walk. That walk checks every tile already on disk, and the
        # short-lived GetShowcaseSweeps signature dies during it. The show page signature lasts longer.
        await downloadOverlayTiles(base_page_text)
        consoleLog(f"Downloading primary model assets has 4k: {SWEEP_DO_4K}...")
        await downloadMainAssets(pageid, accessurl)
    os.chdir(THIS_MODEL_ROOT_DIR)
    generatedCrops = 0
    if CLA.getCommandLineArg(CommandLineArg.GENERATE_TILE_MESH_CROPS):
        consoleLog("Generating tile_mesh crop images locally (no progress shown)...")
        generatedCrops = GenerateMeshImageCrops()

    PROGRESS.ClearRelative()
    consoleLog(f"Done, {PROGRESS} GeneratedCrops: {generatedCrops}!")


def GenerateMeshImageCrops():
    global Image
    from PIL import Image

    models_dir = "models"
    totalGenned = 0
    for model_id in os.listdir(models_dir):
        model_path = os.path.join(models_dir, model_id, "assets", "mesh_tiles", "~")
        if not os.path.exists(model_path):
            return

        for tile_folder in os.listdir(model_path):
            tile_path = os.path.join(model_path, tile_folder)
            if not os.path.isdir(tile_path):
                continue

            # Process each jpg file here
            for file in os.listdir(tile_path):
                if not file.endswith(".jpg") or "crop" in file:
                    continue
                totalGenned += GenerateCrops(os.path.join(tile_path, file))
    return totalGenned


def GenerateCrops(jpgFilePath):
    cropSize = 512
    testFilename = f"{jpgFilePath}crop={cropSize},{cropSize},x0,y0.jpg"
    howMany = 0
    if os.path.exists(testFilename):
        return howMany
    img = Image.open(jpgFilePath)

    maxSize = img.width
    increment = int(maxSize / cropSize)

    for x in range(0, increment):
        for y in range(0, increment):
            xPos = x / increment
            yPos = x / increment
            xPos = round(x / increment, 3)  # computer mathsss
            yPos = round(y / increment, 3)  # computer mathsss
            xPosStr = f"{xPos}"
            yPosStr = f"{yPos}"
            if xPosStr.endswith(".0"):
                xPosStr = xPosStr[:-2]
            if yPosStr.endswith(".0"):
                yPosStr = yPosStr[:-2]
            outFilename = f"{jpgFilePath}crop={cropSize},{cropSize},x{xPosStr},y{yPosStr}.jpg"
            xPos *= maxSize
            yPos *= maxSize

            cropped = img.crop((xPos, yPos, xPos + cropSize, yPos + cropSize))
            cropped.save(outFilename)
            howMany += 1
    return howMany


async def AdvancedAssetDownload(base_page_text: str):
    global MODEL_IS_DEFURNISHED, BASE_MODEL_ID, SWEEP_DO_4K
    ADV_CROP_FETCH = [{"start": "width=512&crop=1024,1024,", "increment": "0.5"}, {"start": "crop=512,512,", "increment": "0.25"}]
    consoleLog("Doing advanced download of dollhouse/floorplan data...")
    # Started to parse the modeldata further.  As it is error prone tried to try catch silently for failures. There is more data here we could use for example:
    # queries.GetModelPrefetch.data.model.locations[X].pano.skyboxes[Y].tileUrlTemplate
    # queries.GetModelPrefetch.data.model.locations[X].pano.skyboxes[Y].urlTemplate
    # queries.GetModelPrefetch.data.model.locations[X].pano.resolutions[Y] <--- has the resolutions they offer for this one
    # goal here is to move away from some of the access url hacks, but if we are successful on try one won't matter:)
    # KeyHandler.EnableDisableKeyReplacement(False)

    try:
        base_node: Any = None  # lets try to use this now first seems to be more accurate the precache key can be invalid in comparison
        base_cache_node: Any = None
        base_node_snapshots: Any = None
        try:
            with open("api/mp/models/graph_GetModelDetails.json", "r", encoding="UTF-8") as f:
                graphModelDetailsJson = json.loads(f.read())
                base_node = graphModelDetailsJson["data"]["model"]
        except Exception:
            logging.exception("Unable to open graph model details output json something probably wrong.....")

        try:
            with open("api/mp/models/graph_GetSnapshots.json", "r", encoding="UTF-8") as f:
                graphModelSnapshotsJson = json.loads(f.read())
                base_node_snapshots = graphModelSnapshotsJson["data"]["model"]
        except Exception:
            logging.exception("Unable to open graph model for snapshots output json something probably wrong.....")

        match = re.search(r"window.MP_PREFETCHED_MODELDATA = (\{.+?\}\}\});", base_page_text)
        preload_json_str = None
        if not match:
            match = re.search(r"window.MP_PREFETCHED_MODELDATA = parseJSON\((\"\{.+?\}\}\}\")\);", base_page_text)  # this happens for extra unicode encoded pages
            consoleDebugLog("Main page embedded preset data was unicode/parseJSON passed instead of normal")
            if not match:
                logging.exception("Unable to open graph model for snapshots output json something probably wrong.....")
                consoleLog("###### UNABLE to extract pre-fetch data from main page, will try to proceed but likely have issues", logging.WARNING)
            else:
                preload_json_str = json.loads(match.group(1))  # yes we load it here first, it is a string passed to parseJson so this basically unescapes that string into preload_json_str which will then be loaded again later
        else:
            preload_json_str = match.group(1)

        if preload_json_str is not None:
            preload_json = json.loads(preload_json_str)  # in theory this json should be similar to GetModelDetails, sometimes it is a bit different so we may want to switch
            base_cache_node = preload_json["queries"]["GetModelPrefetch"]["data"]["model"]
        if CLA.getCommandLineArg(CommandLineArg.DEBUG):
            DebugSaveFile("base_page_extracted_json.json", json.dumps(preload_json, indent="\t"))  # noqa: E701
            DebugSaveFile("base_page_extracted_precache_advanced_model_data.json", json.dumps(base_cache_node, indent="\t"))  # noqa: E701
            DebugSaveFile("advanced_model_data_from_GetModelDetails.json", json.dumps(base_node, indent="\t"))  # noqa: E701
            DebugSaveFile("advanced_model_data_from_GetSnapshots.json", json.dumps(base_node_snapshots, indent="\t"))  # noqa: E701

        if not base_cache_node:
            consoleLog("No embedded cache node falling back to Graph GetModelDetails query", logging.WARNING)
            base_cache_node = base_node
        if not base_node:
            consoleLog("Not sure data query GetModelDetails worked we didn't get json back as expected, generally we don't need it for the dl run as we use embedded cache", logging.INFO)
            base_node = base_cache_node
        if "locations" not in base_node:  # the query doesnt get locations back but the cache does have it, this is expected true
            base_node["locations"] = base_cache_node["locations"]

        if MODEL_IS_DEFURNISHED:
            KeyHandler.SetAccessKey(AccessKeyType.MAIN_PAGE_GENERIC_KEY, "INVALIDGENERICACCESSKEY")
            KeyHandler.SetAccessKey(AccessKeyType.FILES2_BASE_URL_KEY, "INVALIDGENERICACCESSKEYFILE2")
            KeyHandler.SetAccessKey(AccessKeyType.FILES3_TEMPLATE_KEY, "INVALIDGENERICACCESSKEYFILES3")
            KeyHandler.SetAccessKey(AccessKeyType.MAIN_PAGE_DAM_50K, "INVALIDGENERICACCESSKEYDAM")
            # BASE_MODEL_ID = base_cache_node["baseView"]["model"]["id"]
            BASE_MODEL_ID = base_cache_node["views"][0]["model"]["rootModelId"]
            consoleLog("Downloading fix api info for  base model...")
            await downloadFixedAPIInfo(BASE_MODEL_ID)
            KeyHandler.SetAccessKey(AccessKeyType.SWEEP_KEY, KeyHandler.GetKeysFromStr(base_cache_node["locations"][0]["pano"]["skyboxes"][0]["tileUrlTemplate"])[0])

        else:
            KeyHandler.SetAccessKey(AccessKeyType.MAIN_PAGE_GENERIC_KEY, KeyHandler.GetKeysFromStr(base_cache_node["assets"]["textures"][0]["urlTemplate"])[0])
            if base_cache_node["defurnishViews"] and base_cache_node["defurnishViews"][0]:
                consoleLog(f"#### WARNING: Model has defurnished view if you want to download it you must run a second matterport-dl.py download on its id of: {base_cache_node['defurnishViews'][0]['model']['id']}", loglevel=logging.WARN)
        toDownload: list[AsyncDownloadItem] = []
        if base_node_snapshots:
            consoleDebugLog(f"AdvancedDownload photos: {len(base_node_snapshots['assets']['photos'])} meshes: {len(base_node['assets']['meshes'])}, locations: {len(base_node['locations'])}, tileset indexes: {len(base_node['assets']['tilesets'])}, textures: {len(base_node['assets']['textures'])}, ")

        # now: getmodeldetails: data.model.assets.meshes
        # Note if this is actually base_node the damn key won't work, only prefetch one seems to work
        for mesh in base_cache_node["assets"]["meshes"]:  # generally there is 50k and 500k but 500k we dont seem to have access to
            damAccessKey = KeyHandler.GetKeysFromStr(mesh["url"])[0]
            if mesh["resolution"] == "50k":
                KeyHandler.SetAccessKey(AccessKeyType.MAIN_PAGE_DAM_50K, damAccessKey)
            toDownload.append(AsyncDownloadItem("ADV_UUID_DAM", "50k" not in mesh["url"], mesh["url"], urlparse(mesh["url"]).path[1:], key_type=AccessKeyType.LeaveKeyAlone))  # not expecting the non 50k one to work but mgiht as well try

        # the photos and skyboxes similar urls (including working keys) can be found on the api/v1/player/models/ID/index.html file but note the V1 versions .url is not with the access key but .src is

        # now: instead from the snapshots graph data: data.model.assets.photos
        for photo in base_node_snapshots["assets"]["photos"]:
            imageUrl = photo["url"]  # this should be uncropped and unscaled original
            if not imageUrl:
                imageUrl = photo["presentationUrl"]  # fallback not sure we every need this
            toDownload.append(AsyncDownloadItem("ADV_MODEL_IMAGES", True, imageUrl, urlparse(imageUrl).path[1:], key_type=AccessKeyType.LeaveKeyAlone))

        # Download GetModelPrefetch.data.model.locations[X].pano.skyboxes[Y].urlTemplate
        # now: getsweeps graph data: data.model.locations
        resolutionDetectionWarning = ""
        do4K = len(base_node["locations"]) == 0  # by default only do 4k if we have no locations data to check
        for location in base_node["locations"]:
            if "4k" in location["pano"]["resolutions"]:
                do4K = True
            elif do4K:
                if CLA.getCommandLineArg(CommandLineArg.DEBUG):
                    raise Exception("Found a non 4k pano even though others are 4k, dynamic resolution detection needed")
            for skybox in location["pano"]["skyboxes"]:
                if skybox["status"] == "locked":  # not sure why some models have 4k but then locked skyboxes at that resolution.  The normal skybox key also with 403  trying to access the 4ks.  We can use the main key to access the urls but it just gives a 404
                    if skybox["resolution"] == "4k":
                        # do4K = False
                        if not resolutionDetectionWarning:
                            resolutionDetectionWarning = "Found 4k resolution locked for a skybox, but this doesn't seem to be a problem any more"
                    elif CLA.getCommandLineArg(CommandLineArg.DEBUG):
                        resolutionDetectionWarning = "Found non 4k resolution locked for a skybox? Curious if actual download for it will 403..."
                try:
                    for face in range(6):
                        skyboxUrlTemplate = skybox["urlTemplate"].replace("<face>", f"{face}")
                        toDownload.append(AsyncDownloadItem("ADV_SKYBOX", False, skyboxUrlTemplate, urlparse(skyboxUrlTemplate).path[1:], key_type=AccessKeyType.LeaveKeyAlone))
                except:
                    pass
        SWEEP_DO_4K = do4K
        if resolutionDetectionWarning:
            consoleDebugLog(resolutionDetectionWarning)
        consoleLog("Going to download tileset 3d asset models")
        # Download Tilesets
        # now: getmodeldetails: data.model.assets.tilesets
        for tileset in base_cache_node["assets"]["tilesets"]:  # normally just one tileset
            tilesetUrl = tileset["url"]
            tilesetDepth = int(tileset["tilesetDepth"])
            tilesetUrlTemplate: str = tileset["urlTemplate"]
            if "<file>" not in tilesetUrlTemplate:  # the graph details does have it but the cached data does not
                tilesetUrlTemplate = tilesetUrlTemplate.replace("?", "<file>?")
            tilesetBaseFile = urlparse(tilesetUrl).path[1:]
            try:
                tileSetBytes = await downloadFileAndGetText("ADV_TILESET", False, tilesetUrl, tilesetBaseFile, isBinary=True, key_type=AccessKeyType.LeaveKeyAlone)
                tileSetText = tileSetBytes.decode("utf-8", "ignore")
                # tileSetText = validUntilFix(tileSetText)
                # with open(getModifiedName(tilesetBaseFile), "w", encoding="UTF-8") as f:
                # f.write(tileSetText)

                uris = re.findall(r'"uri":"(.+?)"', tileSetText)  # a bit brutish to extract rather than just walking the json

                uris.sort()

                for uri in tqdm(uris):
                    url = tilesetUrlTemplate.replace("<file>", uri)
                    try:
                        chunkBytes = await downloadFileAndGetText("ADV_TILESET_GLB", False, url, urlparse(url).path[1:], isBinary=True, key_type=AccessKeyType.LeaveKeyAlone)
                        chunkText = chunkBytes.decode("utf-8", "ignore")
                        chunks = re.findall(r"(lod[0-9]_[a-zA-Z0-9-_]+\.(jpg|ktx2))", chunkText)
                        # print("Found chunks: ",chunks)
                        chunks.sort()
                        for ktx2 in chunks:
                            chunkUri = f"{uri[:2]}{ktx2[0]}"
                            chunkUrl = tilesetUrlTemplate.replace("<file>", chunkUri)
                            toDownload.append(AsyncDownloadItem("ADV_TILESET_TEXTURE", False, chunkUrl, urlparse(chunkUrl).path[1:], key_type=AccessKeyType.LeaveKeyAlone))

                    except:
                        raise
            except:
                raise

            for file in range(tilesetDepth + 1):
                try:
                    tileseUrlTemplate = tilesetUrlTemplate.replace("<file>", f"{file}.json")
                    getFileText = await downloadFileAndGetText("ADV_TILESET_JSON", False, tileseUrlTemplate, urlparse(tileseUrlTemplate).path[1:], key_type=AccessKeyType.LeaveKeyAlone)
                    fileUris = re.findall(r'"uri":"(.*?)"', getFileText)
                    fileUris.sort()
                    for fileuri in fileUris:
                        fileUrl = tilesetUrlTemplate.replace("<file>", fileuri)
                        try:
                            toDownload.append(AsyncDownloadItem("ADV_TILESET_EXTRACT", False, fileUrl, urlparse(fileUrl).path[1:], key_type=AccessKeyType.LeaveKeyAlone))
                        except:
                            pass

                except:
                    pass

        # now: getmodeldetails: data.model.assets.textures
        for texture in base_node["assets"]["textures"]:
            try:  # on first exception assume we have all the ones needed so cant use array download as need to know which fails (other than for crops)
                for i in range(1000):
                    full_text_url = texture["urlTemplate"].replace("<texture>", f"{i:03d}")
                    crop_to_do = []
                    if texture["quality"] == "high":
                        crop_to_do = ADV_CROP_FETCH

                    try:  # try our full texture first so we can bail out before trying each and every crop
                        await downloadFile("ADV_TEXTURE_FULL", True, full_text_url, urlparse(full_text_url).path[1:])
                    except:
                        break
                    for crop in crop_to_do:
                        for x in list(drange(0, 1, decimal.Decimal(crop["increment"]))):
                            for y in list(drange(0, 1, decimal.Decimal(crop["increment"]))):
                                xs = f"{x}"
                                ys = f"{y}"
                                if xs.endswith(".0"):
                                    xs = xs[:-2]
                                if ys.endswith(".0"):
                                    ys = ys[:-2]
                                complete_add = f"{crop['start']}x{xs},y{ys}"
                                complete_add_file = complete_add.replace("&", "_")
                                toDownload.append(AsyncDownloadItem("ADV_TEXTURE_CROPPED", False, full_text_url + "&" + complete_add, urlparse(full_text_url).path[1:] + complete_add_file + ".jpg"))  # failures here ok we dont know all teh crops that exist, so we can still use the array downloader

            except Exception:
                logging.exception("Adv download texture have exception")
        consoleLog("Downloading textures and previews for tileset 3d models")
        await AsyncArrayDownload(toDownload)
    except Exception:
        logging.exception("Adv download general had exception of")
        if CLA.getCommandLineArg(CommandLineArg.DEBUG):
            raise
        else:
            pass
    # KeyHandler.EnableDisableKeyReplacement(True)


async def initiateDownload(url):
    try:
        async with OUR_SESSION:
            await downloadCapture(getPageId(url))
    except Exception:
        logging.exception("Unhandled fatal exception")
        raise


def getPageId(url):
    global MODEL_IS_DEFURNISHED
    id = url.split("m=")[-1].split("&")[0]
    MODEL_IS_DEFURNISHED = len(id) == 25
    if not id.isalnum() or ((len(id) < 5 or len(id) > 15) and not MODEL_IS_DEFURNISHED):  # 25 can be used for defurnished models
        raise Exception(f"Likely invalid model id extracted: {id} from your input of: {url} you should pass the ID itself (ie EGxFGTFyC9N) or the url: form like: https://my.matterport.com/show/?m=EGxFGTFyC9N")
    return id


SLOW_REQUEST_SECONDS = 8


class ServeRequestTracker:
    """Keeps track of what the browser currently has in flight.

    When the viewer sits on the loading spinner forever the useful question is "what is still
    being waited on", so the server answers that at GET /__diag (the page side watchdog in
    JSNetProxy.js posts there for us too, which lands both sides of the story in server.log)
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.nextId = 0
        self.inFlight: dict[int, dict] = {}
        self.slow = collections.deque(maxlen=40)
        self.recent = collections.deque(maxlen=25)
        self.missing = collections.deque(maxlen=25)
        self.lastRequestStarted = 0.0

    def start(self, method, path):
        with self.lock:
            self.nextId += 1
            id = self.nextId
            self.inFlight[id] = {"method": method, "path": path, "started": time.time()}
            self.lastRequestStarted = time.time()
        return id

    def noteMissing(self, id):
        # A placeholder is HTTP 200 so the viewer can move on. Remember that the file was absent.
        with self.lock:
            item = self.inFlight.get(id)
            if item is None:
                return
            item["missing"] = True
            self.missing.append({"method": item["method"], "path": item["path"]})

    def finish(self, id, status):
        with self.lock:
            item = self.inFlight.pop(id, None)
            if item is None:
                return
            took = item["tookSeconds"] = round(time.time() - item["started"], 2)
            item["status"] = status
            entry = {k: item[k] for k in ("method", "path", "status", "tookSeconds")}
            if item.get("missing"):
                entry["missing"] = True
            self.recent.append(entry)
            if took > SLOW_REQUEST_SECONDS:
                self.slow.append(item)
        if took > SLOW_REQUEST_SECONDS:
            consoleDebugLog(f"Request took {took}s: {item['method']} {item['path']}", loglevel=logging.WARNING)

    def snapshot(self):
        now = time.time()
        with self.lock:
            flight = [{"method": i["method"], "path": i["path"], "ageSeconds": round(now - i["started"], 1)} for i in self.inFlight.values()]
            flight.sort(key=lambda i: -i["ageSeconds"])
            recent = [r for r in self.recent if not r["path"].startswith("/__diag")]
            return {
                "inFlightCount": len(flight),
                "inFlight": flight,
                "secondsSinceLastRequest": round(now - self.lastRequestStarted, 1) if self.lastRequestStarted else None,
                "lastRequests": recent[-12:],
                "missingAssets": list(self.missing)[-12:],
                "slowRequests": list(self.slow)[-10:],
            }


SERVE_REQUEST_TRACKER = ServeRequestTracker()


_PLACEHOLDER_PNG = None


def _getPlaceholderPng():
    """Return a tiny valid 1x1 gray PNG, served for missing image assets so the
    viewer degrades to a glitch (missing tile) instead of hanging on a 404."""
    global _PLACEHOLDER_PNG
    if _PLACEHOLDER_PNG is None:
        import zlib
        import struct

        def _chunk(tag, data):
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        _PLACEHOLDER_PNG = (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(b"\x00\x80\x80\x80"))
            + _chunk(b"IEND", b""))
    return _PLACEHOLDER_PNG


class OurSimpleHTTPRequestHandler(SimpleHTTPRequestHandler):
    # HTTP/1.1 so the browser can reuse a connection for the many parallel asset requests it
    # makes.  With the default of HTTP/1.0 every single asset needs a brand new connection which,
    # combined with a server that can only talk to one connection at a time, is how you end up
    # with a request that never gets an answer (viewer stuck on the loading spinner until reload)
    protocol_version = "HTTP/1.1"
    # Browsers do open connections they never send a request on, don't let those (or a stalled
    # keep alive connection) hold on to a handler thread forever
    timeout = 30
    _sentConnectionHeader = False
    _responseStatus = None
    _trackId = None

    def handle_one_request(self):
        # The timeout only guards an idle keep alive connection while we wait for the next request
        # to arrive, it must never cut off a large asset being written to a reader that is
        # momentarily busy (parse, decode, upload) which is how a viewer can end up half loaded
        self._sentConnectionHeader = False
        self.connection.settimeout(self.timeout)
        try:
            SimpleHTTPRequestHandler.handle_one_request(self)
        except (socket.timeout, TimeoutError):
            self.close_connection = True
        finally:
            SERVE_REQUEST_TRACKER.finish(self._trackId, self._responseStatus)
            self._trackId = None

    def parse_request(self):
        result = SimpleHTTPRequestHandler.parse_request(self)
        self.connection.settimeout(None)
        if result:
            self._responseStatus = None
            self._trackId = SERVE_REQUEST_TRACKER.start(self.command, self.path)
        return result

    def send_response_only(self, code, message=None):
        try:
            self._responseStatus = int(code)
        except (TypeError, ValueError):
            self._responseStatus = code
        SimpleHTTPRequestHandler.send_response_only(self, code, message)

    def log_error(self, format, *args):
        msg = format % args
        if "timed out" in msg:
            consoleDebugLog("Closing an idle connection with no request on it", loglevel=logging.DEBUG)
        else:
            consoleLog(msg, logging.ERROR)

    def respondJson(self, body):
        if type(body) is not bytes:
            body = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        # Content length is required with HTTP/1.1 or the browser sits waiting for a body forever
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        if code == 404:
            consoleLog(f"###### 404 error: {self.path} may not be downloading everything right", logging.WARNING)
            # Serve a placeholder instead of a 404 for a missing model asset so the
            # viewer degrades to a glitch (missing tile) rather than hanging on a
            # request that never returns. A 404 here can leave the viewer waiting
            # on a promise forever (loading spinner only a reload gets out of).
            placeholder = self._placeholderForMissingAsset()
            if placeholder is not None:
                content_type, body = placeholder
                SERVE_REQUEST_TRACKER.noteMissing(self._trackId)
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                # A normal refresh should pick up the real file once it has been downloaded.
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            # Keep the connection. Stock send_error always sends Connection: close and
            # leaves the socket open, so the browser opens a new connection per 404.
            body = self._errorPage(code, message, explain)
            self.send_response(code, message)
            self.send_header("Content-Type", "text/html;charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)
            return
        # Stock send_error tells the client to close. Close the socket too, or the next
        # request on this connection is written to a client that has already hung up.
        self.close_connection = True
        SimpleHTTPRequestHandler.send_error(self, code, message, explain)

    def _errorPage(self, code, message, explain):
        try:
            shortmsg, longmsg = self.responses[code]
        except KeyError:
            shortmsg, longmsg = "???", "???"
        if message is None:
            message = shortmsg
        if explain is None:
            explain = longmsg
        content = (
            "<!DOCTYPE HTML>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            f"<title>Error response</title>\n</head>\n<body>\n<h1>Error response</h1>\n"
            f"<p>Error code: {code}</p>\n<p>Message: {html.escape(str(message))}.</p>\n"
            f"<p>Error code explanation: {code} - {html.escape(str(explain))}.</p>\n</body>\n</html>\n"
        )
        return content.encode("utf-8", "replace")

    def _placeholderForMissingAsset(self):
        """Return (content_type, body) to serve for a missing model asset so the
        viewer degrades gracefully instead of hanging on a 404, or None to keep
        the normal 404 (for paths that are not model assets)."""
        raw_path = self.getRawPath()
        if not raw_path.startswith("/models/"):
            return None
        ext = os.path.splitext(raw_path)[1].lower()
        if ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
            return ("image/png", _getPlaceholderPng())
        if ext == ".json":
            return ("application/json", b"{}")
        # Any other missing model asset: complete the request with an empty body
        # so the viewer does not wait on a promise forever
        return ("application/octet-stream", b"")

    def log_request(self, code="-", size="-"):
        if CLA.getCommandLineArg(CommandLineArg.QUIET) and code == 200:
            return
        SimpleHTTPRequestHandler.log_request(self, code, size)

    def end_headers(self):
        self.send_my_headers()
        if self.close_connection and self.request_version != "HTTP/0.9" and not self._sentConnectionHeader:
            # Say we are hanging up rather than letting the client find out when it goes to
            # reuse the connection (browsers will not retry a post on a connection that vanished)
            self.send_header("Connection", "close")
        SimpleHTTPRequestHandler.end_headers(self)

    def send_header(self, keyword, value):
        if keyword.lower() == "connection":
            self._sentConnectionHeader = True
        SimpleHTTPRequestHandler.send_header(self, keyword, value)

    def do_OPTIONS(self):
        self.send_response(200)
        # Content length is required with HTTP/1.1 or the browser sits waiting for a body forever
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_my_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        if self.isPotentialModifiedFile():
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")

    def getRawPath(self):
        raw_path, _, query = self.path.partition("?")
        return raw_path

    def getQuery(self):
        raw_path, _, query = self.path.partition("?")
        return query

    def do_GET(self):
        global BASE_MATTERPORTDL_DIR
        redirect_msg = None
        orig_request = self.path
        if not CLA.getCommandLineArg(CommandLineArg.TILDE):
            self.path = self.path.replace("~", "_")

        orig_raw_path = raw_path = self.getRawPath()
        query = self.getQuery()
        if raw_path == "/__diag":
            # what the browser is waiting on right now, see ServeRequestTracker above
            self.respondJson(SERVE_REQUEST_TRACKER.snapshot())
            return
        if urlparse(self.path).path == "/api/mp/models/graph":
            query_args = urllib.parse.parse_qs(query)
            self.do_GraphRequest(query_args.get("operationName", [None])[0])
            return

        if raw_path.endswith("/"):
            raw_path += "index.html"

        if raw_path.startswith("/JSNetProxy.js"):
            consoleDebugLog("Using our javascript network proxier", loglevel=logging.INFO)
            with open(os.path.join(BASE_MATTERPORTDL_DIR, "JSNetProxy.js"), "r", encoding="UTF-8") as f:
                proxyText = f.read().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(proxyText)))
            self.end_headers()
            self.wfile.write(proxyText)
            return

        if raw_path.startswith("/locale/messages/strings_") and not os.path.exists(f".{raw_path}"):
            redirect_msg = "original request was for a locale we do not have downloaded"
            raw_path = "/locale/strings.json"

        if "crop=" in query and raw_path.endswith(".jpg"):
            query_args = urllib.parse.parse_qs(query)
            crop_addition = query_args.get("crop", None)
            if crop_addition is not None:
                crop_addition = f"crop={crop_addition[0]}"
            else:
                crop_addition = ""

            width_addition = query_args.get("width", None)
            if width_addition is not None:
                width_addition = f"width={width_addition[0]}_"
            else:
                width_addition = ""
            test_path = raw_path + width_addition + crop_addition + ".jpg"
            if os.path.exists(f".{test_path}"):
                raw_path = test_path
                redirect_msg = "dollhouse/floorplan texture request that we have downloaded, better than generic texture file"
            else:
                consoleDebugLog(f"Requested crop texture not found: {test_path} falling back to full res may show visual issues", loglevel=logging.WARNING)

        if raw_path != orig_raw_path:
            self.path = raw_path
        if self.isPotentialModifiedFile():
            posFile = getModifiedName(self.path)
            if os.path.exists(posFile[1:]):
                self.path = posFile
                redirect_msg = "modified version exists"

        if redirect_msg is not None or orig_request != self.path:
            consoleDebugLog(f"Redirecting {orig_request} => {self.path} as {redirect_msg}", loglevel=logging.INFO)
        SimpleHTTPRequestHandler.do_GET(self)

    def isPotentialModifiedFile(self):
        posModifiedExt = ["js", "json", "html"]
        raw_path = self.getRawPath()
        for ext in posModifiedExt:
            if raw_path.endswith(f".{ext}"):
                return True
        return False

    def do_GraphRequest(self, option_name: str):
        post_msg = None
        logLevel = logging.INFO
        body = bytes('{"data": "empty"}', "utf-8")
        if option_name in GRAPH_DATA_REQ:
            file_path = f"api/mp/models/graph_{option_name}.json"
            if os.path.exists(file_path):
                with open(file_path, "r", encoding="UTF-8") as f:
                    body = f.read().encode("utf-8")
                post_msg = f"graph of operationName: {option_name} we are handling internally"
            else:
                logLevel = logging.WARNING
                post_msg = f"graph for operationName: {option_name} we have no saved response for, returning empty instead. If you get an error this may be why (include this message in bug report)."
        else:
            logLevel = logging.WARNING
            post_msg = f"graph request for operationName: {option_name} is not one we know how to handle, returning empty instead. If you get an error this may be why (include this message in bug report)."
        # Always send a complete response, a request that gets nothing back at all leaves the
        # showcase waiting on a promise forever (loading spinner only a reload gets out of)
        self.respondJson(body)

        if post_msg is not None:
            consoleDebugLog(f"Handling a graph request on {self.path}: {post_msg}", loglevel=logLevel)

    def do_POST(self):
        post_msg = None
        logLevel = logging.INFO
        try:
            content_len = int(self.headers.get("content-length") or "0")
            post_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else ""
            if (self.headers.get("transfer-encoding") or "").lower() == "chunked":
                # We can not consume a chunked body, end this connection so the bytes the
                # client still sends can not be mistaken for the next request on it
                self.close_connection = True
            if urlparse(self.path).path == "/__diag":
                # the page side watchdog of JSNetProxy.js telling us the viewer never came up,
                # log both sides of the story so server.log explains a hang after the fact
                consoleLog(f"Page reports the viewer is stuck: {post_body[:1500]}", logging.WARNING)
                snapshot = SERVE_REQUEST_TRACKER.snapshot()
                consoleLog(f"What the server saw when the page asked: {json.dumps(snapshot)}", logging.WARNING)
                self.respondJson(snapshot)
                return
            if urlparse(self.path).path == "/api/mp/models/graph":
                json_body = json.loads(post_body)
                option_name = json_body["operationName"]
                self.do_GraphRequest(option_name)
                return
        except Exception as error:
            logLevel = logging.ERROR
            post_msg = f"Error trying to handle a post request of: {str(error)} this should not happen"
            pass
        finally:
            if post_msg is not None:
                consoleDebugLog(f"Handling a post request on {self.path}: {post_msg}", loglevel=logLevel)

        self.do_GET()  # just treat the POST as a get otherwise:)

    def guess_type(self, path):
        res = SimpleHTTPRequestHandler.guess_type(self, path)
        if res == "text/html":
            return "text/html; charset=UTF-8"
        return res


GRAPH_DATA_REQ = {
    "GetModelDetails": "?operationName=GetModelDetails&variables=%7B%22modelId%22%3A%22[MATTERPORT_MODEL_ID]%22%7D&extensions=%7B%22persistedQuery%22%3A%7B%22version%22%3A1%2C%22sha256Hash%22%3A%22012c3d36cdf890ba8e49dfd66b1072a2dbb573e672d72482eff86a2563530f46%22%7D%7D",
    "GetModelViewPrefetch": "?operationName=GetModelViewPrefetch&variables=%7B%22modelId%22%3A%22[MATTERPORT_MODEL_ID]%22%2C%22includeDisabled%22%3Afalse%2C%22includeLayers%22%3Atrue%7D&extensions=%7B%22persistedQuery%22%3A%7B%22version%22%3A1%2C%22sha256Hash%22%3A%22ed65e7307756d949f0e7cdab0cf79ee0b0797cc9c494d8811a2bb3025cd7bce6%22%7D%7D",
    "GetRoomBounds": "?operationName=GetRoomBounds&variables=%7B%22modelId%22%3A%22[MATTERPORT_MODEL_ID]%22%7D&extensions=%7B%22persistedQuery%22%3A%7B%22version%22%3A1%2C%22sha256Hash%22%3A%2214f99a0c44512f435987f1305eacdaea7ca600f2b5e9022499087188e63915aa%22%7D%7D",
    "GetShowcaseSweeps": "?operationName=GetShowcaseSweeps&variables=%7B%22modelId%22%3A%22[MATTERPORT_MODEL_ID]%22%7D&extensions=%7B%22persistedQuery%22%3A%7B%22version%22%3A1%2C%22sha256Hash%22%3A%220faff869a8ae9385fe262d18ea1f731bbbeb3d618c036e60a8d0d630ae3526a5%22%7D%7D",
    "GetSnapshots": "?operationName=GetSnapshots&variables=%7B%22modelId%22%3A%22[MATTERPORT_MODEL_ID]%22%7D&extensions=%7B%22persistedQuery%22%3A%7B%22version%22%3A1%2C%22sha256Hash%22%3A%22510bd772b16a48aa4ea74aa290373225eeb306b3162fa34c75d6f643daf3f22b%22%7D%7D",
    "GetModelAssets": "?operationName=GetModelAssets&variables=%7B%22modelId%22%3A%22[MATTERPORT_MODEL_ID]%22%7D&extensions=%7B%22persistedQuery%22%3A%7B%22version%22%3A1%2C%22sha256Hash%22%3A%2288fcfb6cb48b4c99c53623b3d939da42da1a700014fe1e926ba6f14f49b777c2%22%7D%7D",
    "GetSweeps": "?operationName=GetSweeps&variables=%7B%22sweepIds%22%3Anull%2C%22modelId%22%3A%22[MATTERPORT_MODEL_ID]%22%2C%22sweepTags%22%3A%5B%22showcase%22%5D%7D&extensions=%7B%22persistedQuery%22%3A%7B%22version%22%3A1%2C%22sha256Hash%22%3A%22f7f1c9ec3e99b6579e76291da523de689c07d5e2c421f4ab82cf826b19af2427%22%7D%7D",
    # the following normally only seen on defurnished views directly
    "GetRoomClassifications": "?operationName=GetRoomClassifications&variables=%7B%7D&extensions=%7B%22persistedQuery%22%3A%7B%22version%22%3A1%2C%22sha256Hash%22%3A%22fdfefe83b3f6c491b0b76576b34366278274c92b36324bef4dd299d39fb986f3%22%7D%7D",  # yes get room classificaitons does not take a model id
    "GetRooms": "?operationName=GetRooms&variables=%7B%22modelId%22%3A%22[MATTERPORT_MODEL_ID]%22%7D&extensions=%7B%22persistedQuery%22%3A%7B%22version%22%3A1%2C%22sha256Hash%22%3A%223743bbb80ad617297c8e8c943499144b8ff20840a5c1fd48e5d3d131d915ed3a%22%7D%7D",
    "GetFloors": "?operationName=GetFloors&variables=%7B%22modelId%22%3A%22[MATTERPORT_MODEL_ID]%22%7D&extensions=%7B%22persistedQuery%22%3A%7B%22version%22%3A1%2C%22sha256Hash%22%3A%225df6b8c235ad84bba1032135922354e8850320ce8780315993722778d9835f15%22%7D%7D",
    "GetModelOptions": "?operationName=GetModelOptions&variables=%7B%22modelId%22%3A%22[MATTERPORT_MODEL_ID]%22%7D&extensions=%7B%22persistedQuery%22%3A%7B%22version%22%3A1%2C%22sha256Hash%22%3A%2248765f4d53c1700521382c03034c1b67e63b9bdc6cccdc6c4e853620cd9c74c7%22%7D%7D",
}
OUR_SESSION: requests.AsyncSession
MAX_TASKS_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
RUN_ARGS_CONFIG_NAME = "run_args.json"


def SetupSession(use_proxy):
    global OUR_SESSION, MAX_CONCURRENT_REQUESTS, BASE_MATTERPORT_DOMAIN
    OUR_SESSION = requests.AsyncSession(impersonate="chrome", max_clients=MAX_CONCURRENT_REQUESTS, verify=CLA.getCommandLineArg(CommandLineArg.VERIFY_SSL), proxies=({"http": use_proxy, "https": use_proxy} if use_proxy else None), headers={"Referer": f"https://my.{BASE_MATTERPORT_DOMAIN}/", "x-matterport-application-name": "showcase"})


def RegisterWindowsBrowsers():
    """Read the installed browsers from the Windows registry."""
    # https://github.com/python/cpython/issues/52479
    import winreg

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"Software\Clients\StartMenuInternet") as hkey:
        i = 0
        while True:
            try:
                subkey = winreg.EnumKey(hkey, i)
                i += 1
            except OSError as e:
                if e.winerror != 259:  # ERROR_NO_MORE_ITEMS
                    raise
                break
            try:
                name = winreg.QueryValue(hkey, subkey)
                if not name or not isinstance(name, str):
                    name = subkey
            except OSError:
                name = subkey
            try:
                cmd = winreg.QueryValue(hkey, rf"{subkey}\shell\open\command")
                cmd = cmd.strip('"')
                os.stat(cmd)
            except (OSError, AttributeError, TypeError, ValueError):
                cmd = ""
            name = name.replace("Google ", "").replace("Mozilla ", "").replace("Microsoft ", "")  # emulate stock names
            webbrowser.register(name, None, webbrowser.GenericBrowser(cmd))


class OurThreadingHTTPServer(ThreadingHTTPServer):
    # One thread per connection.  The viewer opens a lot of connections at once (and some of
    # them it never sends a request on) so a single threaded server ends up sitting on an idle
    # connection while the requests actually waiting on an answer never get served.
    daemon_threads = True
    # HTTPServer turns this on. On Windows SO_REUSEADDR lets a second process bind this
    # same port while the first is still listening, and the two then split requests.
    allow_reuse_address = False
    # The stock default of 5 is far too small for the burst of connections a browser opens
    request_queue_size = 128

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionError, TimeoutError)):
            # Browser gave up on (or cancelled) a request, nothing to report
            logging.debug(f"Connection with {client_address} ended early: {error}")
        else:
            logging.exception(f"Error while handling a request from {client_address}")


def startServer(baseDir, pageId, browserLaunch, bindAddress, bindPort):
    global SERVED_BASE_URL
    # pageId is already a model id, or an alias folder name. getPageId only accepts model ids.
    twinDir = pageId
    if not os.path.exists(twinDir):
        fullPath = os.path.abspath(twinDir)
        relativeToScriptDir = os.path.join(BASE_MATTERPORTDL_DIR, baseDir, twinDir)
        if os.path.exists(relativeToScriptDir):
            os.chdir(relativeToScriptDir)
        else:
            raise Exception(f"Unable to change to download directory for twin of: {fullPath} or {os.path.abspath(relativeToScriptDir)} make sure the download is there")
    else:
        os.chdir(twinDir)
    # force=True is required: in interactive mode consoleLog() has already run before we get
    # here, and because the root logger had no handlers Python's logging auto-called
    # basicConfig() which installed a stderr StreamHandler.  Without force=True that
    # pre-existing handler makes this basicConfig a no-op, so server.log is never created
    # and nothing is ever written to it.  force=True clears the old handlers first.
    try:
        logging.basicConfig(filename="server.log", filemode="w", encoding="utf-8", level=logging.DEBUG, format="%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S", force=True)
    except ValueError:
        logging.basicConfig(filename="server.log", filemode="w", level=logging.DEBUG, format="%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S", force=True)

    if CLA.getCommandLineArg(CommandLineArg.CONSOLE_LOG):
        logging.getLogger().addHandler(logging.StreamHandler())

    SERVED_BASE_URL = url = f"http://{bindAddress}:{bindPort}"
    logging.info(f"Server starting up {sys_info()}")
    # People look for this file constantly when a load goes wrong (and wonder whether it is even
    # live) so record where it is, what is served, and which process is the one writing it
    logging.info(f"Serving {os.getcwd()} at {url} from process {os.getpid()}, log at {os.path.abspath('server.log')}")
    print(f"Serving {os.getcwd()} at {url}\nLogging to: {os.path.abspath('server.log')} (process {os.getpid()})")
    print("View in browser: " + url)
    httpd = OurThreadingHTTPServer((bindAddress, bindPort), OurSimpleHTTPRequestHandler)
    if browserLaunch:
        print(f"Going to try and launch browser type: {browserLaunch}")
        import webbrowser

        if sys.platform == "win32":
            RegisterWindowsBrowsers()
        webbrowser.get(browserLaunch).open_new_tab(url)
    httpd.serve_forever()


class KeyHandler:
    # not actually used currently
    # KEY_REPLACE_ACTIVE : ClassVar[bool] = True #no longer toggling this

    # Primary key we use normally MAIN_PAGE_GENERIC_KEY but if adv download is off fallback to FILES3_TEMPLATE_KEY which generally works but can have a shorter lifetime
    PrimaryKey: ClassVar[str] = None

    RE_ACCESS_KEY_EXTRACT: ClassVar[re.Pattern] = re.compile(r"2\-[0-9a-z]{40}\-17[0-9]{8}-[0-9]")  # important to remain case sensitive
    # This is every key we have run into not generally used for anyhting but brute force and explaining where keys come from
    KNOWN_ACCESS_KEYS: ClassVar[dict[str, str]] = {}  # key to source(s) of key
    # most resources work with our main page generic key but the dam file uses the dam key
    ACCESS_KEYS_BY_TYPE: ClassVar[dict[AccessKeyType, str]] = {}

    @staticmethod
    def GetAllKeys() -> list[str]:
        return list(KeyHandler.KNOWN_ACCESS_KEYS.keys())

    @staticmethod
    def GetAccessKey(key_type: AccessKeyType):
        return KeyHandler.ACCESS_KEYS_BY_TYPE[key_type]

    @staticmethod
    def SetAccessKey(key_type: AccessKeyType, key: str):
        if type(key) is not str or not key:
            raise Exception(f"Call with invalid key for SetAccessKey {key_type} = {key}")
        consoleDebugLog(f"SetAccessKey for {key_type} = {key}")
        KeyHandler.ACCESS_KEYS_BY_TYPE[key_type] = key
        if key_type == AccessKeyType.MAIN_PAGE_GENERIC_KEY:
            KeyHandler.PrimaryKey = key
        if KeyHandler.PrimaryKey is None and key_type == AccessKeyType.FILES3_TEMPLATE_KEY:  # our former primary key
            KeyHandler.PrimaryKey = key

    @staticmethod
    def GetKeysFromStr(parseText) -> list[str]:
        return KeyHandler.RE_ACCESS_KEY_EXTRACT.findall(parseText)

    # fromWhat should not have any spaces
    @staticmethod
    def SaveKeysFromText(fromWhat, text):
        foundKeys = KeyHandler.GetKeysFromStr(text)
        textDescriptor = f" {fromWhat} "
        for foundKey in foundKeys:
            if foundKey in KeyHandler.KNOWN_ACCESS_KEYS:
                if textDescriptor in KeyHandler.KNOWN_ACCESS_KEYS[foundKey]:
                    continue
            else:
                KeyHandler.KNOWN_ACCESS_KEYS[foundKey] = " "
            KeyHandler.KNOWN_ACCESS_KEYS[foundKey] += fromWhat + " "
        # 2-3394fea8af5bf96264fd16b21267e779c6aaf4cb-1735546220-0
        # all access keys right now start with a 2 a dash then a 40 char hash a dash then a timestamp starting with 17 (wont go to 18 until 2027) then a single digit, single digit at end maybe indicate purpose?

    @staticmethod
    def DumpKnownKeysToFile():
        toSort = []
        for key in KeyHandler.KNOWN_ACCESS_KEYS:
            keyDesc = KeyHandler.KNOWN_ACCESS_KEYS[key].strip()
            outStr = f"T {key.split('-')[-1]}: {keyDesc} - {key}"
            toSort.append(outStr)
        toSort.sort()
        DebugSaveFile("keys.txt", "\n".join(toSort))

    # print all keys that work for url
    @staticmethod
    async def PrintUrlKeys(url, isDownload):
        # turnKeyReplacementBackOn=False
        workingKeys = ""
        # if KeyHandler.KEY_REPLACE_ACTIVE:
        # KeyHandler.EnableDisableKeyReplacement(False)
        # turnKeyReplacementBackOn=True
        # toDownload.append(AsyncDownloadItem(type, shouldExist, f"{base}{asset}", local_file))
        consoleLog("Finding url keys....")
        toDownload: list[AsyncDownloadItem] = []
        existingKey = KeyHandler.GetKeysFromStr(url)
        if len(existingKey) > 0:
            KeyHandler.KNOWN_ACCESS_KEYS["ORIG"] = existingKey[0]
        async with aiofiles.tempfile.TemporaryDirectory() as tmp:
            print(f"Directory is: {tmp}")
            pathlib.Path(os.path.join(tmp, "test")).touch()
            haveSavedDownload = False
            debugTargetName = os.path.join("debug", os.path.basename(urlparse(url).path[1:]))
            for key in KeyHandler.KNOWN_ACCESS_KEYS:
                toDownload.append(AsyncDownloadItem("FindUrlKey", False, KeyHandler.SetAccessKeyForUrl(url, key), os.path.join(tmp, "test_" + key), key_type=AccessKeyType.LeaveKeyAlone))
            await AsyncArrayDownload(toDownload)
            for _file in os.listdir(tmp):
                if not _file.startswith("test_"):
                    continue
                if isDownload:
                    if not haveSavedDownload:
                        haveSavedDownload = True
                        shutil.copy(os.path.join(tmp, _file), debugTargetName)
                file = _file[5:]
                workingKeys += f"\t{file}({KeyHandler.KNOWN_ACCESS_KEYS[file].strip()})\n"

        # if turnKeyReplacementBackOn:
        # EnableDisableKeyReplacement(True)
        consoleLog(f"### FOR URL: {url} ACCESS KEYS THAT WORK:\n{workingKeys}")

    @staticmethod
    def SetAccessKeyForUrl(url: str, key_val: str, addIfMissing=False):
        match = KeyHandler.RE_ACCESS_KEY_EXTRACT.search(url)

        if match is None:
            if addIfMissing:
                if "?" in url:
                    return url + "&t=" + key_val
                else:
                    return url + "?t=" + key_val
            return url
        return url.replace(match.group(0), key_val)


CommandLineArg = Enum("CommandLineArg", ["ADVANCED_DOWNLOAD", "PROXY", "VERIFY_SSL", "DEBUG", "CONSOLE_LOG", "TILDE", "BASE_FOLDER", "ALIAS", "DOWNLOAD", "MAIN_ASSET_DOWNLOAD", "MANUAL_HOST_REPLACEMENT", "ALWAYS_DOWNLOAD_GRAPH_REQS", "QUIET", "HELP", "ADV_HELP", "AUTO_SERVE", "FIND_URL_KEY", "FIND_URL_KEY_AND_DOWNLOAD", "REFRESH_KEY_FILES", "GENERATE_TILE_MESH_CROPS", "TITLE"])
ArgAppliesTo = Enum("ArgAppliesTo", ["DOWNLOAD", "SERVING", "BOTH"])


@dataclass
class CLA:
    arg: CommandLineArg
    description: str
    hasValue: bool
    itemValueHelpDisplay: str
    defaultValue: Any
    currentValue: Any
    hidden: bool
    allow_saving: bool
    applies_to: ArgAppliesTo
    all_args: ClassVar[list[CLA]] = []
    orig_args: ClassVar[list[str]] = []  # we store them so we can reparse them after a config load
    value_cache: ClassVar[dict[CommandLineArg, Any]] = {}  # faster lookup

    @staticmethod
    def addCommandLineArg(arg: CommandLineArg, description: str, defaultValue: Any, itemValueHelpDisplay: str = "", hidden=False, allow_saved=True, applies_to=ArgAppliesTo.DOWNLOAD):
        """itemValueHelpDisplay is the name to show in help for after the --arg   ie for --proxy '127.0.0.1:8080'"""
        cla = CLA(arg=arg, currentValue=defaultValue, defaultValue=defaultValue, description=description, hasValue=itemValueHelpDisplay != "", itemValueHelpDisplay=itemValueHelpDisplay, hidden=hidden, allow_saving=allow_saved, applies_to=applies_to)
        if len(CLA.orig_args) == 0:
            CLA.orig_args = sys.argv.copy()
        for i in range(len(sys.argv) - 1, -1, -1):
            isNegativeName = sys.argv[i] == f"--no-{cla.argConsoleName()}"
            if sys.argv[i] == f"--{cla.argConsoleName()}" or isNegativeName:
                sys.argv.pop(i)
                if cla.hasValue and not isNegativeName:
                    sys.argv.pop(i)
        CLA.all_args.append(cla)

    @staticmethod
    def parseArgs():
        CLA.value_cache = {}
        for i in range(1, len(CLA.orig_args)):
            for cla in CLA.all_args:
                isNegativeName = CLA.orig_args[i] == f"--no-{cla.argConsoleName()}"
                if CLA.orig_args[i] == f"--{cla.argConsoleName()}" or isNegativeName:
                    if cla.hasValue:
                        cla.currentValue = CLA.orig_args[i + 1] if not isNegativeName else ""
                    else:
                        cla.currentValue = not isNegativeName

    def argConsoleName(self):
        return self.arg.name.replace("_", "-").lower()

    @staticmethod
    def LoadFromFile(file: str):
        if CLA.getCommandLineArg(CommandLineArg.DEBUG):
            print(f"Loaded config from {file} val")
        with open(file, "r", encoding="UTF-8") as f:
            config = json.loads(f.read())
            for arg in CLA.all_args:
                if arg.arg.name in config:
                    CLA.setCommandLineArg(arg.arg, config[arg.arg.name])

    @staticmethod
    def SaveToFile(file: str):
        config: dict[str, Any] = {}
        for arg in CLA.all_args:
            if arg.allow_saving:
                config[arg.arg.name] = arg.currentValue
        with open(file, "w") as the_file:
            the_file.write(json.dumps(config, indent="\t"))

    @staticmethod
    def getUsageStr(indent=2, forServerNotDownload=False):
        ret = ""
        for arg in CLA.all_args:
            noprefix = ""
            if arg.hidden and not CLA.getCommandLineArg(CommandLineArg.ADV_HELP):
                continue
            if CLA.getCommandLineArg(CommandLineArg.ADV_HELP) and not arg.hidden:
                continue
            if forServerNotDownload:
                if arg.applies_to == ArgAppliesTo.DOWNLOAD:
                    continue
            elif arg.applies_to == ArgAppliesTo.SERVING:
                continue

            desc = arg.description

            if arg.currentValue:
                if not arg.hasValue:
                    noprefix = "no-"
                    desc = f"disables: {desc}"
                else:
                    desc = f"{desc} currently: {arg.currentValue}"

            for _ in range(indent):
                ret += "\t"
            ret += f"--{noprefix}{arg.argConsoleName()} {arg.itemValueHelpDisplay} - {desc}\n"
        return ret.rstrip()

    @staticmethod
    def getCommandLineArg(arg: CommandLineArg):
        if arg in CLA.value_cache:
            return CLA.value_cache[arg]
        cla = next(filter(lambda c: c.arg == arg, CLA.all_args), None)
        if not cla:
            raise Exception(f"Invalid command line arg requested???: {arg}")
        CLA.value_cache[arg] = cla.currentValue
        return cla.currentValue

    @staticmethod
    def setCommandLineArg(arg: CommandLineArg, value: Any):
        CLA.value_cache.pop(arg, None)  # Clear cache entry if exists
        cla = next(filter(lambda c: c.arg == arg, CLA.all_args), None)
        if not cla:
            raise Exception(f"Invalid command line arg requested???: {arg}")
        cla.currentValue = value


DEFAULTS_JSON_FILE = "defaults.json"


def main():
    CLA.addCommandLineArg(CommandLineArg.BASE_FOLDER, "folder to store downloaded models in (or serve from)", "./downloads", itemValueHelpDisplay="dir", allow_saved=False, applies_to=ArgAppliesTo.BOTH)
    CLA.addCommandLineArg(CommandLineArg.PROXY, "using web proxy specified for all requests, use --no-verify-ssl to disable ssl verification", "", "127.0.0.1:8866", allow_saved=False)
    CLA.addCommandLineArg(CommandLineArg.TILDE, "allowing tildes on file paths, likely must be disabled for Apple/Linux, you must use the same option during the capture and serving", sys.platform == "win32")
    CLA.addCommandLineArg(CommandLineArg.ALIAS, "create an alias symlink for the download with this name, does not override any existing (can be used when serving)", "", itemValueHelpDisplay="name")
    CLA.addCommandLineArg(CommandLineArg.ADVANCED_DOWNLOAD, "downloading advanced assets enables things like skyboxes, dollhouse, floorplan layouts, now primary access keys come from it so generally required", True)
    CLA.addCommandLineArg(CommandLineArg.DEBUG, "debug mode enables select debug output to console or the debug/ folder mostly for developers", False, allow_saved=False)
    CLA.addCommandLineArg(CommandLineArg.CONSOLE_LOG, "showing all log messages in the console rather than just the log file, very spammy", False, allow_saved=False)

    CLA.addCommandLineArg(CommandLineArg.DOWNLOAD, "Download items (without this it just does post download actions)", True, hidden=True, allow_saved=False)
    CLA.addCommandLineArg(CommandLineArg.VERIFY_SSL, "SSL verification, mostly useful for proxy situations", True, allow_saved=False, hidden=True)
    CLA.addCommandLineArg(CommandLineArg.MAIN_ASSET_DOWNLOAD, "Primary asset downloads (normally biggest part of the download)", True, hidden=True, allow_saved=False)
    CLA.addCommandLineArg(CommandLineArg.ALWAYS_DOWNLOAD_GRAPH_REQS, "Always download/make graphql requests, a good idea as they have important keys, note if REFRESH_KEY_FILES is off it will still prevent graph files from downloading", True, hidden=True, allow_saved=False)
    CLA.addCommandLineArg(CommandLineArg.FIND_URL_KEY, "A URL to try to find the access key for, makes a few minimal requests upfront to get needed keys", "", "https://my.matterport.com/api/player/models/EGxFGTFyC9N/test.file", hidden=True, allow_saved=False)
    CLA.addCommandLineArg(CommandLineArg.FIND_URL_KEY_AND_DOWNLOAD, "Like FIND_URL_KEY but saves a copy to the debug folder of the item", "", "https://my.matterport.com/api/player/models/EGxFGTFyC9N/test.file", hidden=True, allow_saved=False)
    CLA.addCommandLineArg(CommandLineArg.REFRESH_KEY_FILES, "There are about a half dozen files always downloaded as they may contain access keys we need, this prevents these from downloading", True, hidden=True, allow_saved=False)
    CLA.addCommandLineArg(CommandLineArg.GENERATE_TILE_MESH_CROPS, "Certain views like dollhouse require cropped versions of certain textures, this uses python to generate all those", True, hidden=False, allow_saved=True)

    CLA.addCommandLineArg(CommandLineArg.MANUAL_HOST_REPLACEMENT, "Use old style replacement of matterport URLs rather than the JS proxy, this likely only works if hosted on port 8080 after", False, hidden=True)

    CLA.addCommandLineArg(CommandLineArg.QUIET, "Only show failure log message items when serving", False, applies_to=ArgAppliesTo.SERVING, allow_saved=False)
    CLA.addCommandLineArg(CommandLineArg.AUTO_SERVE, "Used to automatically start the server hosting a specific file, see README for details", "", "page_id_or_alias|host|port|what-browser", applies_to=ArgAppliesTo.SERVING, hidden=True)

    CLA.addCommandLineArg(CommandLineArg.HELP, "", False, hidden=True, allow_saved=False)
    CLA.addCommandLineArg(CommandLineArg.TITLE, "Model title override, normally extracted from page title", "", hidden=True)
    CLA.addCommandLineArg(CommandLineArg.ADV_HELP, "Show advanced command line options normally hidden, not recommended for most users", False, hidden=False, allow_saved=False, applies_to=ArgAppliesTo.BOTH)
    CLA.parseArgs()
    browserLaunch = ""
    defaults_full_path = os.path.join(BASE_MATTERPORTDL_DIR, DEFAULTS_JSON_FILE)
    if os.path.exists(defaults_full_path):
        CLA.LoadFromFile(defaults_full_path)
        CLA.parseArgs()
        autoServe = CLA.getCommandLineArg(CommandLineArg.AUTO_SERVE)
        if autoServe:
            arr = autoServe.split("|")
            if len(arr) > 2:
                if len(arr) == 4:
                    browserLaunch = arr.pop()
                arr.insert(0, "matterport-dl.py")
                sys.argv = arr

    baseDir = CLA.getCommandLineArg(CommandLineArg.BASE_FOLDER)

    SetupSession(CLA.getCommandLineArg(CommandLineArg.PROXY))
    pageId = ""
    bindIp = "127.0.0.1"
    bindPort = 8080
    argPos = 1
    isServerRun = False
    isDownloadRun = False
    subProcessArgs = CLA.orig_args.copy()  # args we give the interactive UI minus ip and port as it only uses them for downloads
    if len(sys.argv) > argPos:
        pageIdOrIp = sys.argv[argPos]
        # Check if pageIdOrIp is an IP address
        ip_pattern = re.compile(r"^(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$")
        if not ip_pattern.match(pageIdOrIp):
            # A renamed model is a symlink under the download folder. Its name is a title, not a model id.
            # A server run of a folder that is already there uses that name. A download still goes through getPageId.
            existing_download = os.path.join(baseDir, pageIdOrIp)
            if os.path.isdir(existing_download) and len(sys.argv) == argPos + 3:
                pageId = pageIdOrIp
            else:
                pageId = getPageId(pageIdOrIp)
            argPos += 1
            if len(sys.argv) == argPos:  # if no more args its a download run
                isDownloadRun = True
        if len(sys.argv) == argPos + 2:  # ip and port left, note if it was an IP above we didn't increment argPos
            isServerRun = pageId != ""
            bindIp = sys.argv[argPos]
            subProcessArgs.remove(bindIp)
            argPos += 1
            bindPort = sys.argv[argPos]
            subProcessArgs.remove(bindPort)
            argPos += 1
            bindPort = int(bindPort)

    if not os.path.exists(os.path.join(baseDir, pageId)) and os.path.exists(pageId) and isServerRun:  # allow old rooted pages to still be served
        baseDir = "./"
    elif isServerRun or isDownloadRun:
        makeDirs(baseDir)
        os.chdir(baseDir)

    existingConfigFile = os.path.join(pageId, RUN_ARGS_CONFIG_NAME)
    if os.path.exists(existingConfigFile):
        try:
            CLA.LoadFromFile(existingConfigFile)
            CLA.parseArgs()
        except:
            pass
    isExplicitHelpCLI = CLA.getCommandLineArg(CommandLineArg.HELP) or CLA.getCommandLineArg(CommandLineArg.ADV_HELP)
    if isExplicitHelpCLI or (not isServerRun and not isDownloadRun):
        consoleLog(sys_info())
        if not isExplicitHelpCLI and sys.stdin.isatty():
            try:
                sys.path.insert(0, str(BASE_MATTERPORTDL_DIR))
                from _matterport_interactive import interactiveManagerGetToServe, print_colored, bcolors

                print("Running in interactive mode.\n\tIf you instead wanted command line usage start this script with: ", end="")
                print_colored(f"{SCRIPT_NAME} --help", bcolors.WARNING)

                pageId = interactiveManagerGetToServe(baseDir, subProcessArgs)

                if pageId:
                    isServerRun = True

            except ImportError:
                print("Error: Could not import interactive start from _matterport_interactive.py")

        else:
            print(f"Usage:\n{SCRIPT_NAME} - Interactive terminal UI mode, any options below will still be passed to any downloads or server starts\n{SCRIPT_NAME} [url_or_page_id] - Download mode, to download the digital twin\n{SCRIPT_NAME} [url_or_page_id_or_alias] 127.0.0.1 8080 - Server mode after downloading will serve the twin just and open http://127.0.0.1:8080 in a browser\n\tThe following options apply to the download run options:")
            print(CLA.getUsageStr())
            print("\tServing options:")
            print(CLA.getUsageStr(forServerNotDownload=True))
            print("\tAny option can have a no prefix added (or removed if already has) to invert the option,  ie --no-proxy disables a proxy if one was enabled.  --no-advanced-download disables the default enabled advanced download.")
            sys.exit(1)

    if isDownloadRun:
        asyncio.run(initiateDownload(pageId))

    if isServerRun:
        startServer(baseDir, pageId, browserLaunch, bindIp, bindPort)


if __name__ == "__main__":
    main()
