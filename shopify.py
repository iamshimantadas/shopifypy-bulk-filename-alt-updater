
import csv
import os
import sys
import time
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

import requests
from dotenv import load_dotenv


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

SHOPIFY_STORE_DOMAIN = os.getenv("SHOPIFY_STORE_DOMAIN", "").strip()
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "").strip()
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2026-10").strip()

INPUT_CSV = "tableConvert.com_fqdir4.csv"
REPORT_CSV = "shopify_update_report.csv"

# False = preview only; no Shopify changes.
# True  = actually update filenames and alt text.
APPLY_CHANGES = True

PAGE_SIZE = 100
REQUEST_TIMEOUT = 60
MAX_RETRIES = 3

if not SHOPIFY_STORE_DOMAIN or not SHOPIFY_ACCESS_TOKEN:
    sys.exit(
        "ERROR: Set SHOPIFY_STORE_DOMAIN and "
        "SHOPIFY_ACCESS_TOKEN in your .env file."
    )

API_URL = (
    f"https://{SHOPIFY_STORE_DOMAIN}/admin/api/"
    f"{SHOPIFY_API_VERSION}/graphql.json"
)

HEADERS = {
    "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
    "Content-Type": "application/json",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ============================================================
# GRAPHQL
# ============================================================

SEARCH_FILES_QUERY = """
query SearchFiles($first: Int!, $after: String, $query: String!) {
  files(first: $first, after: $after, query: $query) {
    nodes {
      __typename
      id
      alt
      fileStatus

      ... on MediaImage {
        image {
          url
        }
      }

      ... on GenericFile {
        url
      }

      ... on Video {
        originalSource {
          url
        }
      }
    }

    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

UPDATE_FILES_MUTATION = """
mutation UpdateFiles($files: [FileUpdateInput!]!) {
  fileUpdate(files: $files) {
    files {
      __typename
      id
      alt
      fileStatus

      ... on MediaImage {
        image {
          url
        }
      }

      ... on GenericFile {
        url
      }
    }

    userErrors {
      field
      message
    }
  }
}
"""


def graphql(query, variables):
    """Execute a Shopify GraphQL request with basic retries."""

    for attempt in range(MAX_RETRIES):
        try:
            response = SESSION.post(
                API_URL,
                json={
                    "query": query,
                    "variables": variables,
                },
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code == 429:
                wait = int(response.headers.get("Retry-After", "2"))
                time.sleep(max(wait, 1))
                continue

            response.raise_for_status()
            payload = response.json()

            if payload.get("errors"):
                raise RuntimeError(
                    f"Shopify GraphQL errors: {payload['errors']}"
                )

            if "data" not in payload:
                raise RuntimeError(
                    f"Unexpected Shopify response: {payload}"
                )

            return payload["data"]

        except (requests.RequestException, ValueError) as exc:
            if attempt == MAX_RETRIES - 1:
                raise RuntimeError(
                    f"Shopify request failed: {exc}"
                ) from exc

            time.sleep(2 ** attempt)

    raise RuntimeError("Shopify request failed after retries.")


# ============================================================
# CSV INPUT
# ============================================================

def load_csv(filename):
    """Load and validate the three-column input CSV."""

    if not os.path.isfile(filename):
        raise FileNotFoundError(
            f"Input CSV not found: {filename}"
        )

    required_columns = {
        "old_file_name",
        "new_file_name",
        "alt_text",
    }

    rows = []
    seen_old_names = set()
    seen_new_names = set()

    with open(filename, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        if not reader.fieldnames:
            raise ValueError("The input CSV is empty.")

        reader.fieldnames = [
            (name or "").strip() for name in reader.fieldnames
        ]

        missing = required_columns - set(reader.fieldnames)

        if missing:
            raise ValueError(
                "CSV is missing required columns: "
                + ", ".join(sorted(missing))
            )

        for line_number, row in enumerate(reader, start=2):
            old_name = (row.get("old_file_name") or "").strip()
            new_stem = (row.get("new_file_name") or "").strip()
            alt_text = (row.get("alt_text") or "").strip()

            if not old_name and not new_stem and not alt_text:
                continue

            if not old_name or not new_stem:
                print(
                    f"[SKIP] CSV line {line_number}: "
                    "old_file_name and new_file_name are required."
                )
                continue

            if "/" in old_name or "\\" in old_name:
                print(
                    f"[SKIP] CSV line {line_number}: "
                    "old_file_name must be a filename, not a path."
                )
                continue

            if "/" in new_stem or "\\" in new_stem:
                print(
                    f"[SKIP] CSV line {line_number}: "
                    "new_file_name must not contain a path."
                )
                continue

            if not PurePosixPath(old_name).suffix:
                print(
                    f"[SKIP] CSV line {line_number}: "
                    "old_file_name must include its extension."
                )
                continue

            extension = PurePosixPath(old_name).suffix

            # If the extension was accidentally included in column 2,
            # remove it before adding the original extension.
            if new_stem.lower().endswith(extension.lower()):
                new_stem = new_stem[:-len(extension)]

            if not new_stem or new_stem in {".", ".."}:
                print(
                    f"[SKIP] CSV line {line_number}: "
                    "invalid new filename."
                )
                continue

            # Shopify filenames should not contain path separators.
            if any(char in new_stem for char in '<>:"|?*'):
                print(
                    f"[SKIP] CSV line {line_number}: "
                    "new filename contains invalid characters."
                )
                continue

            final_new_name = new_stem + extension

            old_key = old_name.casefold()
            new_key = final_new_name.casefold()

            if old_key in seen_old_names:
                print(
                    f"[SKIP] CSV line {line_number}: "
                    f"duplicate old filename: {old_name}"
                )
                continue

            if new_key in seen_new_names:
                print(
                    f"[SKIP] CSV line {line_number}: "
                    f"duplicate target filename: {final_new_name}"
                )
                continue

            seen_old_names.add(old_key)
            seen_new_names.add(new_key)

            rows.append({
                "line_number": line_number,
                "old_file_name": old_name,
                "new_file_name": final_new_name,
                "alt_text": alt_text,
            })

    # A target filename must not collide with another row's old filename.
    # This prevents unsafe rename chains and swaps.
    old_to_new = {
        row["old_file_name"].casefold():
        row["new_file_name"].casefold()
        for row in rows
    }

    for row in rows:
        target_key = row["new_file_name"].casefold()

        if (
            target_key in old_to_new
            and target_key != row["old_file_name"].casefold()
        ):
            raise ValueError(
                f"Rename conflict: {row['new_file_name']} is also "
                "an old filename in the CSV. Resolve this conflict "
                "before running the script."
            )

    return rows


# ============================================================
# FILE SEARCH
# ============================================================

def get_file_url(file_node):
    """Return the URL for supported Shopify file types."""

    file_type = file_node.get("__typename")

    if file_type == "MediaImage":
        image = file_node.get("image") or {}
        return image.get("url", "")

    if file_type == "GenericFile":
        return file_node.get("url", "")

    if file_type == "Video":
        source = file_node.get("originalSource") or {}
        return source.get("url", "")

    return ""


def filename_from_url(url):
    """Extract a filename from a Shopify file URL."""

    if not url:
        return ""

    path = unquote(urlparse(url).path)
    return PurePosixPath(path).name


def search_files_by_prefix(filename):
    """
    Search Shopify using a filename prefix, paginate all results,
    and return the results for that prefix.
    """

    extension = PurePosixPath(filename).suffix
    prefix = filename[:-len(extension)] if extension else filename

    search_expression = f"filename:{prefix}*"

    results = []
    cursor = None

    while True:
        data = graphql(
            SEARCH_FILES_QUERY,
            {
                "first": PAGE_SIZE,
                "after": cursor,
                "query": search_expression,
            },
        )

        connection = data["files"]
        results.extend(connection["nodes"])

        page_info = connection["pageInfo"]

        if not page_info["hasNextPage"]:
            break

        cursor = page_info["endCursor"]

        if not cursor:
            raise RuntimeError(
                "Shopify reported another page but returned no cursor."
            )

    return results


# Cache searches so repeated filename prefixes are not fetched repeatedly.
SEARCH_CACHE = {}


def find_exact_files(filename):
    """Find exact filename matches, ignoring case."""

    extension = PurePosixPath(filename).suffix
    prefix = filename[:-len(extension)] if extension else filename

    if prefix not in SEARCH_CACHE:
        SEARCH_CACHE[prefix] = search_files_by_prefix(filename)

    matches = []

    for node in SEARCH_CACHE[prefix]:
        actual_name = filename_from_url(get_file_url(node))

        if actual_name.casefold() == filename.casefold():
            matches.append(node)

    return matches


# ============================================================
# REPORT
# ============================================================

REPORT_COLUMNS = [
    "old_file_name",
    "new_file_name",
    "alt_text",
    "result",
    "shopify_id",
    "current_filename",
    "current_alt",
    "current_status",
    "url",
    "details",
]


def write_report(report_rows):
    with open(
        REPORT_CSV,
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=REPORT_COLUMNS,
        )
        writer.writeheader()
        writer.writerows(report_rows)

    print(f"\nReport saved: {REPORT_CSV}")


# ============================================================
# MAIN UPDATE PROCESS
# ============================================================

def main():
    try:
        input_rows = load_csv(INPUT_CSV)
    except Exception as exc:
        sys.exit(f"CSV ERROR: {exc}")

    if not input_rows:
        sys.exit("No valid rows found in the input CSV.")

    print(f"\nInput rows: {len(input_rows)}")
    print(f"Shopify store: {SHOPIFY_STORE_DOMAIN}")
    print(f"Live updates enabled: {APPLY_CHANGES}")
    print("-" * 75)

    report = []
    plans = []
    selected_ids = set()

    # --------------------------------------------------------
    # STEP 1: SEARCH AND VALIDATE EVERY INPUT ROW
    # No changes are made in this step.
    # --------------------------------------------------------

    for row in input_rows:
        old_name = row["old_file_name"]
        new_name = row["new_file_name"]
        alt_text = row["alt_text"]

        print(f"\nSearching: {old_name}")

        base_report = {
            "old_file_name": old_name,
            "new_file_name": new_name,
            "alt_text": alt_text,
            "result": "",
            "shopify_id": "",
            "current_filename": "",
            "current_alt": "",
            "current_status": "",
            "url": "",
            "details": "",
        }

        try:
            matches = find_exact_files(old_name)
        except Exception as exc:
            base_report["result"] = "SEARCH_ERROR"
            base_report["details"] = str(exc)
            report.append(base_report)
            print(f"[ERROR] Search failed: {exc}")
            continue

        if len(matches) == 0:
            base_report["result"] = "NOT_FOUND"
            base_report["details"] = (
                "No exact filename match found in Shopify."
            )
            report.append(base_report)
            print("[SKIP] No exact match found.")
            continue

        if len(matches) > 1:
            base_report["result"] = "AMBIGUOUS"
            base_report["details"] = (
                f"Found {len(matches)} exact matches; "
                "nothing was updated."
            )
            report.append(base_report)
            print(
                f"[SKIP] Found {len(matches)} exact matches. "
                "Manual review required."
            )
            continue

        file_node = matches[0]
        file_id = file_node["id"]
        file_type = file_node.get("__typename", "")
        current_url = get_file_url(file_node)
        current_name = filename_from_url(current_url)
        current_status = file_node.get("fileStatus", "")

        base_report.update({
            "shopify_id": file_id,
            "current_filename": current_name,
            "current_alt": file_node.get("alt") or "",
            "current_status": current_status,
            "url": current_url,
        })

        if file_id in selected_ids:
            base_report["result"] = "DUPLICATE_FILE_ID"
            base_report["details"] = (
                "This Shopify file was already selected by another row."
            )
            report.append(base_report)
            print("[SKIP] Same Shopify file selected more than once.")
            continue

        selected_ids.add(file_id)

        # This script updates only image and generic files.
        if file_type not in ("MediaImage", "GenericFile"):
            base_report["result"] = "UNSUPPORTED_TYPE"
            base_report["details"] = (
                f"File type {file_type} is not supported for "
                "filename updates by this script."
            )
            report.append(base_report)
            print(f"[SKIP] Unsupported file type: {file_type}")
            continue

        if current_status != "READY":
            base_report["result"] = "NOT_READY"
            base_report["details"] = (
                f"Shopify file status is {current_status}, not READY."
            )
            report.append(base_report)
            print(f"[SKIP] File status is {current_status}.")
            continue

        old_extension = PurePosixPath(current_name).suffix.casefold()
        new_extension = PurePosixPath(new_name).suffix.casefold()

        if old_extension != new_extension:
            base_report["result"] = "EXTENSION_MISMATCH"
            base_report["details"] = (
                "The new filename must preserve the original extension."
            )
            report.append(base_report)
            print("[SKIP] Extension mismatch.")
            continue

        plans.append({
            "input": row,
            "node": file_node,
            "report": base_report,
        })

        print(
            f"[FOUND] {current_name} | "
            f"ID: {file_id} | Status: {current_status}"
        )

    # --------------------------------------------------------
    # STEP 2: CHECK TARGET FILENAMES FOR COLLISIONS
    # --------------------------------------------------------

    safe_plans = []

    for plan in plans:
        row = plan["input"]
        report_row = plan["report"]
        target_name = row["new_file_name"]
        old_name = row["old_file_name"]

        # No filename change is needed, but alt text may change.
        if target_name.casefold() == old_name.casefold():
            safe_plans.append(plan)
            continue

        try:
            target_matches = find_exact_files(target_name)
        except Exception as exc:
            report_row["result"] = "COLLISION_CHECK_ERROR"
            report_row["details"] = (
                f"Could not check target filename: {exc}"
            )
            print(
                f"[ERROR] Could not check target {target_name}: {exc}"
            )
            continue

        conflicting_matches = [
            item for item in target_matches
            if item["id"] != report_row["shopify_id"]
        ]

        if conflicting_matches:
            report_row["result"] = "TARGET_EXISTS"
            report_row["details"] = (
                "Another Shopify file already uses the target filename."
            )
            print(
                f"[SKIP] Target filename already exists: {target_name}"
            )
            continue

        safe_plans.append(plan)

    # --------------------------------------------------------
    # STEP 3: PREVIEW
    # --------------------------------------------------------

    print("\n" + "=" * 75)
    print("UPDATE PREVIEW")
    print("=" * 75)

    for plan in safe_plans:
        row = plan["input"]
        node = plan["node"]

        print(f"\nOld filename : {row['old_file_name']}")
        print(f"New filename : {row['new_file_name']}")
        print(f"Old alt text : {node.get('alt') or ''}")
        print(f"New alt text : {row['alt_text']}")
        print(f"Shopify ID   : {node['id']}")

    if not APPLY_CHANGES:
        for plan in safe_plans:
            report_row = plan["report"]
            report_row["result"] = "DRY_RUN"
            report_row["details"] = (
                "Preview only; no changes were made."
            )

        for report_row in report:
            if not report_row["result"]:
                report_row["result"] = "NOT_PROCESSED"

        write_report(report)

        print(
            "\nDRY RUN COMPLETE. No Shopify files were changed.\n"
            "Review the report and preview. To apply changes, set "
            "APPLY_CHANGES = True and run the script again."
        )
        return

    if not safe_plans:
        write_report(report)
        print("\nNo eligible files to update.")
        return

    # Explicit confirmation is required for live updates.
    print(
        f"\nYou are about to update {len(safe_plans)} Shopify file(s)."
    )
    confirmation = input(
        'Type UPDATE to confirm live changes: '
    ).strip()

    if confirmation != "UPDATE":
        for plan in safe_plans:
            plan["report"]["result"] = "CANCELLED"
            plan["report"]["details"] = (
                "Live update was not confirmed."
            )

        for report_row in report:
            if not report_row["result"]:
                report_row["result"] = "NOT_PROCESSED"

        write_report(report)
        print("Cancelled. No files were changed.")
        return

    # --------------------------------------------------------
    # STEP 4: UPDATE SHOPIFY
    # --------------------------------------------------------

    for plan in safe_plans:
        row = plan["input"]
        report_row = plan["report"]

        print(f"\nUpdating: {row['old_file_name']}")

        variables = {
            "files": [{
                "id": plan["node"]["id"],
                "filename": row["new_file_name"],
                "alt": row["alt_text"],
            }]
        }

        try:
            data = graphql(UPDATE_FILES_MUTATION, variables)
            result = data["fileUpdate"]

            user_errors = result.get("userErrors") or []

            if user_errors:
                report_row["result"] = "UPDATE_ERROR"
                report_row["details"] = "; ".join(
                    error.get("message", str(error))
                    for error in user_errors
                )
                print(f"[ERROR] {report_row['details']}")
                continue

            updated_files = result.get("files") or []

            if not updated_files:
                report_row["result"] = "UPDATE_UNCONFIRMED"
                report_row["details"] = (
                    "Shopify returned no updated file. "
                    "Verify the file in Shopify."
                )
                print("[WARNING] No updated file returned.")
                continue

            updated = updated_files[0]
            updated_url = get_file_url(updated)

            report_row.update({
                "result": "UPDATED",
                "shopify_id": updated.get("id", ""),
                "current_filename": (
                    filename_from_url(updated_url)
                    or row["new_file_name"]
                ),
                "current_alt": updated.get("alt") or "",
                "current_status": updated.get("fileStatus", ""),
                "url": updated_url,
                "details": "Filename and alt text updated.",
            })

            print("[SUCCESS] File updated.")

        except Exception as exc:
            report_row["result"] = "UPDATE_ERROR"
            report_row["details"] = str(exc)
            print(f"[ERROR] {exc}")

    for report_row in report:
        if not report_row["result"]:
            report_row["result"] = "NOT_PROCESSED"

    write_report(report)

    updated_count = sum(
        1 for item in report
        if item["result"] == "UPDATED"
    )

    failed_count = sum(
        1 for item in report
        if item["result"] not in ("UPDATED",)
    )

    print("\n" + "=" * 75)
    print("PROCESS COMPLETE")
    print(f"Successfully updated: {updated_count}")
    print(f"Skipped or failed:   {failed_count}")
    print(f"Report:              {REPORT_CSV}")
    print("=" * 75)


if __name__ == "__main__":
    main()
