
import os
import re
import csv
import requests
from urllib.parse import urlparse, unquote
from dotenv import load_dotenv

load_dotenv()

STORE_DOMAIN = os.getenv("SHOPIFY_STORE_DOMAIN", "").strip()
ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "").strip()
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2026-10").strip()

if not STORE_DOMAIN or not ACCESS_TOKEN:
    raise SystemExit(
        "Please configure SHOPIFY_STORE_DOMAIN and "
        "SHOPIFY_ACCESS_TOKEN in your .env file."
    )

ENDPOINT = (
    f"https://{STORE_DOMAIN}/admin/api/"
    f"{API_VERSION}/graphql.json"
)

HEADERS = {
    "Content-Type": "application/json",
    "X-Shopify-Access-Token": ACCESS_TOKEN,
}

FILE_NAMES = [
    "bloom__page--contact-01.jpg",
    "bloom__blog-02.jpg",
    "bloom__page--about-video-01.jpg",
    "bloom__blog-05.jpg",
    "bloom__popup-01--Aug2020.jpg",
    "Main_b13ad453-477c-4ed1-9b43-81f3345adfd6.jpg",
    "Main_d624f226-0a89-4fe1-b333-0d1548b43c06.jpg"
]

REPORT_FILE = "shopify_file_search_report.csv"

GRAPHQL_QUERY = """
query SearchFiles($first: Int!, $after: String, $search: String!) {
  files(first: $first, after: $after, query: $search) {
    edges {
      node {
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
    }

    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""


def graphql_request(search, after=None):
    response = requests.post(
        ENDPOINT,
        headers=HEADERS,
        json={
            "query": GRAPHQL_QUERY,
            "variables": {
                "first": 100,
                "after": after,
                "search": search,
            },
        },
        timeout=60,
    )

    response.raise_for_status()
    payload = response.json()

    if payload.get("errors"):
        raise RuntimeError(str(payload["errors"]))

    if not payload.get("data"):
        raise RuntimeError(f"Unexpected API response: {payload}")

    return payload["data"]["files"]


def get_file_url(node):
    kind = node.get("__typename")

    if kind == "MediaImage":
        return (node.get("image") or {}).get("url")

    if kind == "GenericFile":
        return node.get("url")

    if kind == "Video":
        return (node.get("originalSource") or {}).get("url")

    return None


def filename_from_url(url):
    if not url:
        return ""

    path = urlparse(url).path
    return unquote(path.rsplit("/", 1)[-1])


def search_prefix(filename):
    match = re.match(r"[A-Za-z0-9]+", filename)

    if not match:
        raise ValueError(
            f"Cannot derive a Shopify search prefix from {filename!r}"
        )

    return f"filename:{match.group(0)}*"


def fetch_candidates(search):
    """Fetch every page returned by the Shopify search filter."""
    records = []
    cursor = None

    while True:
        result = graphql_request(search, cursor)

        for edge in result["edges"]:
            node = edge["node"]
            url = get_file_url(node)

            records.append({
                "id": node.get("id", ""),
                "type": node.get("__typename", ""),
                "url": url or "",
                "filename": filename_from_url(url),
                "alt": node.get("alt") or "",
                "status": node.get("fileStatus") or "",
            })

        page_info = result["pageInfo"]

        if not page_info["hasNextPage"]:
            break

        cursor = page_info["endCursor"]

    return records


def classify_filename(requested, candidates):
    """
    Exact matches are case-insensitive but otherwise require the
    complete filename, including extension, to match.

    Similar filenames are reported as candidates only.
    They are never selected for updating.
    """
    exact = [
        item for item in candidates
        if item["filename"].casefold() == requested.casefold()
    ]

    # Deduplicate by Shopify file ID.
    exact_by_id = {item["id"]: item for item in exact}
    exact = list(exact_by_id.values())

    if len(exact) == 1:
        return "UNIQUE MATCH", exact, []

    if len(exact) > 1:
        return "DUPLICATE EXACT MATCH", exact, []

    requested_stem = os.path.splitext(requested)[0].casefold()

    similar = []
    for item in candidates:
        candidate_name = item["filename"]
        candidate_stem = os.path.splitext(candidate_name)[0].casefold()

        # Similarity rules are intentionally conservative:
        # - one stem starts with the other, or
        # - the names share a substantial prefix.
        starts_similarly = (
            candidate_stem.startswith(requested_stem)
            or requested_stem.startswith(candidate_stem)
        )

        if starts_similarly and candidate_name.casefold() != requested.casefold():
            similar.append(item)

    # Deduplicate similar candidates.
    similar_by_id = {item["id"]: item for item in similar}
    similar = list(similar_by_id.values())

    if similar:
        return "AMBIGUOUS / SIMILAR", [], similar

    return "NOT FOUND", [], []


def write_report(rows):
    columns = [
        "requested_filename",
        "result",
        "shopify_file_id",
        "matched_filename",
        "url",
        "alt_text",
        "file_status",
        "similar_candidates",
    ]

    with open(REPORT_FILE, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def main():
    print("Connecting to Shopify Admin API...")
    print(f"Store: {STORE_DOMAIN}")
    print(f"Files to search: {len(FILE_NAMES)}")
    print("=" * 75)

    # Search each distinct prefix only once.
    prefix_cache = {}
    report_rows = []

    for requested in FILE_NAMES:
        print(f"\nSearch filename: {requested}")

        try:
            search = search_prefix(requested)

            if search not in prefix_cache:
                prefix_cache[search] = fetch_candidates(search)

            candidates = prefix_cache[search]

            print(f"Shopify search filter: {search}")
            print(f"Candidate records returned: {len(candidates)}")

            status, exact, similar = classify_filename(
                requested, candidates
            )

            print(f"RESULT: {status}")

            if status == "UNIQUE MATCH":
                item = exact[0]

                print(f"  Shopify ID : {item['id']}")
                print(f"  Filename   : {item['filename']}")
                print(f"  URL        : {item['url']}")
                print(f"  Alt text   : {item['alt']}")
                print(f"  Status     : {item['status']}")

                report_rows.append({
                    "requested_filename": requested,
                    "result": status,
                    "shopify_file_id": item["id"],
                    "matched_filename": item["filename"],
                    "url": item["url"],
                    "alt_text": item["alt"],
                    "file_status": item["status"],
                    "similar_candidates": "",
                })

            elif status == "DUPLICATE EXACT MATCH":
                print("Multiple Shopify records have this exact filename.")
                print("Skipping to avoid choosing the wrong file.")

                for item in exact:
                    print(f"  {item['filename']} | {item['id']} | {item['url']}")

                    report_rows.append({
                        "requested_filename": requested,
                        "result": status,
                        "shopify_file_id": item["id"],
                        "matched_filename": item["filename"],
                        "url": item["url"],
                        "alt_text": item["alt"],
                        "file_status": item["status"],
                        "similar_candidates": "",
                    })

            elif status == "AMBIGUOUS / SIMILAR":
                print("No exact match. Similar files found; skipping:")

                similar_text = " || ".join(
                    f"{item['filename']} | {item['id']} | {item['url']}"
                    for item in similar
                )

                for item in similar:
                    print(f"  {item['filename']}")
                    print(f"    ID: {item['id']}")
                    print(f"    URL: {item['url']}")

                report_rows.append({
                    "requested_filename": requested,
                    "result": status,
                    "shopify_file_id": "",
                    "matched_filename": "",
                    "url": "",
                    "alt_text": "",
                    "file_status": "",
                    "similar_candidates": similar_text,
                })

            else:
                print("No exact or similar filename found.")

                report_rows.append({
                    "requested_filename": requested,
                    "result": status,
                    "shopify_file_id": "",
                    "matched_filename": "",
                    "url": "",
                    "alt_text": "",
                    "file_status": "",
                    "similar_candidates": "",
                })

        except Exception as exc:
            print(f"ERROR: {exc}")

            report_rows.append({
                "requested_filename": requested,
                "result": f"ERROR: {exc}",
                "shopify_file_id": "",
                "matched_filename": "",
                "url": "",
                "alt_text": "",
                "file_status": "",
                "similar_candidates": "",
            })

    write_report(report_rows)

    print("\n" + "=" * 75)
    print("SEARCH SUMMARY")

    for row in report_rows:
        print(f"{row['requested_filename']} -> {row['result']}")

    print(f"\nCSV report saved to: {REPORT_FILE}")
    print("No Shopify files were modified.")


if __name__ == "__main__":
    main()
