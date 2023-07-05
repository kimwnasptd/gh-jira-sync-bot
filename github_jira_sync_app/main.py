import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from fastapi import Body
from fastapi import FastAPI
from fastapi import HTTPException
from github import Github
from github import GithubException
from github import GithubIntegration
from jira import JIRA
from mistletoe import Document  # type: ignore[import]
from mistletoe.contrib.jira_renderer import JIRARenderer  # type: ignore[import]
from starlette.requests import Request
from yaml.scanner import ScannerError

jira_text_renderer = JIRARenderer()

load_dotenv()

jira_instance_url = os.getenv("JIRA_INSTANCE", "")
jira_username = os.getenv("JIRA_USERNAME", "")
jira_token = os.getenv("JIRA_TOKEN", "")

assert jira_instance_url, "URL to your Jira instance must be provided via JIRA_INSTANCE env var"
assert jira_username, "Jira username must be provided via JIRA_USERNAME env var"
assert jira_token, "Jira API token must be provided via JIRA_TOKEN env var"

jira_issue_description_template = """
This issue was created from GitHub Issue {gh_issue_url}
Issue was submitted by: {gh_issue_author}

PLEASE KEEP ALL THE CONVERSATION ON GITHUB

{gh_issue_body}
"""

gh_comment_body_template = """
Thank you for reporting us your feedback!

The internal ticket has been created: {jira_issue_link}.

> This message was autogenerated
"""


def define_logger():
    """Define logger to output to the file and to STDOUT."""
    log = logging.getLogger("sync-bot-server")
    log.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        fmt="%(asctime)s (%(levelname)s) %(message)s", datefmt="%d.%m.%Y %H:%M:%S"
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    log.addHandler(stream_handler)

    log_file = os.environ.get("SYNC_BOT_LOGFILE", "sync_bot.log")
    file_handler = logging.FileHandler(filename=log_file)
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)
    return log


logger = define_logger()


with open(Path(__file__).parent / "settings.yaml") as file:
    _file_settings = yaml.safe_load(file)

_env_settings = json.loads(os.getenv("DEFAULT_BOT_CONFIG", "{}"))

DEFAULT_SETTINGS = _env_settings or _file_settings

app_id = os.getenv("APP_ID", "")
app_key = os.getenv("PRIVATE_KEY", "")
app_key = app_key.replace("\\n", "\n")  # since docker env variables do not support multiline

app = FastAPI()


def merge_dicts(d1, d2):
    """Merge the two dictionaries (d2 into d1) recursively.

    If the key from d2 exists in d1, then skip (do not override).

    Mutates d1
    """
    for key in d2:
        if key in d1 and isinstance(d1[key], dict) and isinstance(d2[key], dict):
            merge_dicts(d1[key], d2[key])
        elif key not in d1:
            d1[key] = d2[key]


def verify_signature(payload_body, secret_token, signature_header):
    """Verify that the payload was sent from GitHub by validating SHA256.

    Raise and return 403 if not authorized.

    Args:
        payload_body: original request body to verify (request.body())
        secret_token: GitHub app webhook token (WEBHOOK_SECRET)
        signature_header: header received from GitHub (x-hub-signature-256)

    """
    if not signature_header:
        raise HTTPException(status_code=403, detail="x-hub-signature-256 header is missing!")

    hash_object = hmac.new(secret_token.encode("utf-8"), msg=payload_body, digestmod=hashlib.sha256)
    expected_signature = "sha256=" + hash_object.hexdigest()
    if not hmac.compare_digest(expected_signature, signature_header):
        raise HTTPException(status_code=403, detail="Request signatures didn't match!")


@app.post("/")
async def bot(request: Request, payload: dict = Body(...)):
    body_ = await request.body()
    signature_ = request.headers.get("x-hub-signature-256")

    verify_signature(body_, os.getenv("WEBHOOK_SECRET"), signature_)

    # Check if the event is a GitHub PR creation event
    if not all(k in payload.keys() for k in ["action", "issue"]) and payload["action"] == "opened":
        return "ok"

    if payload["sender"]["login"] == os.getenv("BOT_NAME"):
        # do not handle bot's actions
        return {"msg": "Action was triggered by bot. Ignoring."}

    if payload["action"] in ["deleted", "unlabeled"]:
        # do not handle deletion of comments/issues and unlabeling
        return "ok"

    if payload["action"] == "edited" and "comment" in payload.keys():
        # do not handle modification of comments
        return "ok"

    owner = payload["repository"]["owner"]["login"]
    repo_name = payload["repository"]["name"]

    # keep it here until https://github.com/PyGithub/PyGithub/issues/2431 is fixed
    git_integration = GithubIntegration(
        app_id,
        app_key,
    )
    git_connection = Github(
        login_or_token=git_integration.get_access_token(
            git_integration.get_repo_installation(owner, repo_name).id
        ).token
    )
    repo = git_connection.get_repo(f"{owner}/{repo_name}")
    issue = repo.get_issue(number=payload["issue"]["number"])
    try:
        contents = repo.get_contents(".github/.jira_sync_config.yaml")
        settings_content = contents.decoded_content  # type: ignore[union-attr]
    except GithubException:
        logger.error("Settings file was not found")
        issue.create_comment(".github/.jira_sync_config.yaml file was not found")
        return "ok"

    try:
        settings = yaml.safe_load(settings_content)
    except ScannerError:
        logger.error("YAML file is invalid")
        issue.create_comment(".github/.jira_sync_config.yaml file is invalid. Check syntax.")
        return "ok"

    merge_dicts(settings, DEFAULT_SETTINGS)

    settings = settings["settings"]

    if not settings["jira_project_key"]:
        issue.create_comment(
            "Jira project key is not specified. Add `jira_project_key` key to the settings file."
        )
        return "ok"

    if not settings["status_mapping"]:
        issue.create_comment(
            "Status mapping is not specified. Add `status_mapping` key to the settings file."
        )
        return "ok"

    allowed_labels = [label.lower() for label in settings["labels"]]
    payload_labels = [label["name"].lower() for label in payload["issue"]["labels"]]
    if allowed_labels and not any(label in allowed_labels for label in payload_labels):
        logger.info("Issue is not labeled with the specified label")
        return "ok"

    jira = JIRA(jira_instance_url, basic_auth=(jira_username, jira_token))
    existing_issues = jira.search_issues(
        f'project={settings["jira_project_key"]} AND description ~ "{issue.html_url}"',
        json_result=False,
    )
    assert isinstance(existing_issues, list), "Jira did not return a list of existing issues"
    issue_body = issue.body if settings["sync_description"] else ""
    if issue_body:
        doc = Document(issue_body)
        issue_body = jira_text_renderer.render(doc)

    issue_description = jira_issue_description_template.format(
        gh_issue_url=issue.html_url,
        gh_issue_author=issue.user.login,
        gh_issue_body=issue_body,
    )

    issue_type = "Bug"
    if settings["label_mapping"]:
        for label in payload_labels:
            if label in settings["label_mapping"]:
                issue_type = settings["label_mapping"][label]
                break

    issue_dict: dict[str, Any] = {
        "project": {"key": settings["jira_project_key"]},
        "summary": issue.title,
        "description": issue_description,
        "issuetype": {"name": issue_type},
    }
    if settings["epic_key"]:
        issue_dict["parent"] = {"key": settings["epic_key"]}

    if settings["components"]:
        allowed_components = [c.name for c in jira.project_components(settings["jira_project_key"])]

        issue_dict["components"] = [
            {"name": component}
            for component in settings["components"]
            if component in allowed_components
        ]

    opened_status = settings["status_mapping"]["opened"]
    closed_status = settings["status_mapping"]["closed"]

    if not existing_issues:
        if payload["action"] == "closed":
            return "ok"

        new_issue = jira.create_issue(fields=issue_dict)
        existing_issues.append(new_issue)

        if settings["add_gh_comment"]:
            issue.create_comment(
                gh_comment_body_template.format(jira_issue_link=new_issue.permalink())
            )
    else:
        jira_issue = existing_issues[0]
        if payload["action"] == "closed":
            jira.transition_issue(jira_issue, closed_status)
        elif payload["action"] == "reopened":
            jira.transition_issue(jira_issue, opened_status)
        elif payload["action"] == "edited":
            if settings["components"]:
                # need to append components to the existing list
                for component in jira_issue.fields.components:
                    issue_dict["components"].append({"name": component.name})

            jira_issue.update(fields=issue_dict)

    if settings["sync_comments"] and payload["action"] == "created" and "comment" in payload.keys():
        # new comment was added to the issue

        comment_body = payload["comment"]["body"]
        doc = Document(comment_body)
        comment_body = jira_text_renderer.render(doc)
        jira.add_comment(
            existing_issues[0],
            f"User *{payload['sender']['login']}* commented:\n {comment_body}",
        )
        return "ok"

    return "ok"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=3000)
