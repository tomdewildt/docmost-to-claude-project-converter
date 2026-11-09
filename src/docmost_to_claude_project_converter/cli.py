import argparse
from pathlib import Path

from docmost_to_claude_project_converter.claude import create_claude_client, extract_claude_project_id
from docmost_to_claude_project_converter.config import config
from docmost_to_claude_project_converter.docmost import create_docmost_client, extract_docmost_page_id
from docmost_to_claude_project_converter.exceptions import UnknownCommandError
from docmost_to_claude_project_converter.pdf import PatchrightPdfRenderer
from docmost_to_claude_project_converter.service import ConversionService


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert Docmost pages to Claude project knowledge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    login_parser = subparsers.add_parser("login", help="Sign in to Claude and save the browser session")

    pull_parser = subparsers.add_parser("pull", help="Export a page and its subpages")
    pull_parser.add_argument("id", metavar="PAGE_ID_OR_URL", help="Docmost page id or URL")

    push_parser = subparsers.add_parser("push", help="Upload the exported pages to a Claude project")
    push_parser.add_argument("project", metavar="PROJECT_ID_OR_URL", help="Claude project id or URL")

    sync_parser = subparsers.add_parser("sync", help="Pull, push and remove all other project knowledge")
    sync_parser.add_argument("id", metavar="PAGE_ID_OR_URL", help="Docmost page id or URL")
    sync_parser.add_argument("project", metavar="PROJECT_ID_OR_URL", help="Claude project id or URL")

    parser.set_defaults(output_directory=config.DOCMOST_OUTPUT_DIRECTORY, session_path=config.CLAUDE_SESSION_PATH)
    for command_parser in (pull_parser, push_parser, sync_parser):
        command_parser.add_argument(
            "--output-directory",
            type=Path,
            default=config.DOCMOST_OUTPUT_DIRECTORY,
            help="Output directory",
        )
    for command_parser in (login_parser, push_parser, sync_parser):
        command_parser.add_argument(
            "--session-path",
            type=Path,
            default=config.CLAUDE_SESSION_PATH,
            help="Claude session file",
        )

    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    docmost_client = create_docmost_client(config)
    claude_client = create_claude_client(config.model_copy(update={"CLAUDE_SESSION_PATH": args.session_path}))
    patchright_pdf_renderer = PatchrightPdfRenderer()
    service = ConversionService(docmost_client, claude_client, patchright_pdf_renderer, args.output_directory)

    try:
        match args.command:
            case "login":
                service.login()
            case "pull":
                service.pull(extract_docmost_page_id(args.id))
            case "push":
                service.push(extract_claude_project_id(args.project))
            case "sync":
                service.sync(extract_claude_project_id(args.project), extract_docmost_page_id(args.id))
            case _:
                raise UnknownCommandError(args.command)
    finally:
        claude_client.close()


if __name__ == "__main__":
    main()
