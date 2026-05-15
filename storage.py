"""
storage.py — File upload helper with DigitalOcean Spaces support.

If SPACES_KEY / SPACES_SECRET / SPACES_BUCKET env vars are set, files are
uploaded to DigitalOcean Spaces (S3-compatible) and a permanent public URL is
returned.  Otherwise files are saved to the local uploads/ folder (Replit dev /
single-instance only).

Required env vars for Spaces (set in DigitalOcean App Platform → Settings → Environment):
  SPACES_KEY       Spaces access key
  SPACES_SECRET    Spaces secret key
  SPACES_BUCKET    Bucket / Space name  (e.g. jhehaul-uploads)
  SPACES_REGION    Region slug          (default: nyc3)
  SPACES_ENDPOINT  Full endpoint URL    (default: https://<region>.digitaloceanspaces.com)
  SPACES_CDN_URL   Optional CDN origin  (e.g. https://jhehaul-uploads.nyc3.cdn.digitaloceanspaces.com)
"""

import io
import os
import uuid
import logging

_CONTENT_TYPES = {
    "jpg":  "image/jpeg",
    "jpeg": "image/jpeg",
    "png":  "image/png",
    "gif":  "image/gif",
    "webp": "image/webp",
    "heic": "image/heic",
    "heif": "image/heif",
    "bmp":  "image/bmp",
    "tiff": "image/tiff",
}


def _content_type(ext: str) -> str:
    return _CONTENT_TYPES.get(ext.lower().lstrip("."), "application/octet-stream")


def upload_file(file_obj, ext: str) -> tuple[str, str | None]:
    """
    Persist an uploaded file.

    Parameters
    ----------
    file_obj : werkzeug.datastructures.FileStorage
        The file received from request.files.
    ext : str
        File extension including the dot, e.g. ".jpg".

    Returns
    -------
    (filename, storage_url)
        filename     — UUID-based name used as the DB key.
        storage_url  — Full public URL when saved to Spaces; None when saved locally.
                       Templates should render:
                           photo.storage_url or url_for('uploaded_file', filename=photo.filename)
    """
    filename = f"{uuid.uuid4().hex}{ext.lower()}"

    spaces_key      = os.environ.get("SPACES_KEY")
    spaces_secret   = os.environ.get("SPACES_SECRET")
    spaces_bucket   = os.environ.get("SPACES_BUCKET")
    spaces_region   = os.environ.get("SPACES_REGION", "nyc3")
    spaces_endpoint = os.environ.get(
        "SPACES_ENDPOINT",
        f"https://{spaces_region}.digitaloceanspaces.com",
    )

    if spaces_key and spaces_secret and spaces_bucket:
        try:
            import boto3
            from botocore.client import Config

            # Read into memory first so the stream can be rewound if we fall back
            file_obj.stream.seek(0)
            data = file_obj.stream.read()

            client = boto3.session.Session().client(
                "s3",
                region_name=spaces_region,
                endpoint_url=spaces_endpoint,
                aws_access_key_id=spaces_key,
                aws_secret_access_key=spaces_secret,
                config=Config(signature_version="s3v4"),
            )
            client.upload_fileobj(
                io.BytesIO(data),
                spaces_bucket,
                f"uploads/{filename}",
                ExtraArgs={
                    "ACL": "public-read",
                    "ContentType": _content_type(ext),
                },
            )

            cdn = os.environ.get("SPACES_CDN_URL", "").rstrip("/")
            if cdn:
                storage_url = f"{cdn}/uploads/{filename}"
            else:
                storage_url = (
                    f"{spaces_endpoint.rstrip('/')}/{spaces_bucket}/uploads/{filename}"
                )

            logging.info("storage: uploaded to Spaces → %s", storage_url)
            return filename, storage_url

        except Exception as exc:
            logging.error(
                "storage: Spaces upload failed (%s) — falling back to local filesystem",
                exc,
            )

    # ── Local filesystem fallback (Replit dev / environments without Spaces) ──
    from app import UPLOAD_FOLDER

    save_path = os.path.join(UPLOAD_FOLDER, filename)
    file_obj.stream.seek(0)
    file_obj.save(save_path)
    logging.info("storage: saved locally → uploads/%s", filename)
    return filename, None
