"""
Security utilities for password validation, file validation, and rate limiting.
"""
import re
import os
from typing import Tuple, Optional


class PasswordValidator:
    """Validates password complexity requirements."""

    MIN_LENGTH = 8
    REQUIRE_UPPERCASE = True
    REQUIRE_LOWERCASE = True
    REQUIRE_DIGIT = True
    REQUIRE_SPECIAL = True

    SPECIAL_CHARS = "!@#$%^&*()_+-=[]{}|;:,.<>?"

    @classmethod
    def validate(cls, password: str) -> Tuple[bool, Optional[str]]:
        """
        Validate password against security requirements.

        Returns:
            (is_valid, error_message)
        """
        if len(password) < cls.MIN_LENGTH:
            return False, f"Password must be at least {cls.MIN_LENGTH} characters long"

        if cls.REQUIRE_UPPERCASE and not re.search(r'[A-Z]', password):
            return False, "Password must contain at least one uppercase letter"

        if cls.REQUIRE_LOWERCASE and not re.search(r'[a-z]', password):
            return False, "Password must contain at least one lowercase letter"

        if cls.REQUIRE_DIGIT and not re.search(r'\d', password):
            return False, "Password must contain at least one digit"

        if cls.REQUIRE_SPECIAL and not any(c in cls.SPECIAL_CHARS for c in password):
            return False, f"Password must contain at least one special character ({cls.SPECIAL_CHARS})"

        # Check for common patterns
        if password.lower() in ['password', 'password123', '12345678', 'admin123']:
            return False, "Password is too common. Please choose a stronger password"

        return True, None


class FileValidator:
    """Validates uploaded files for security."""

    MAX_FILE_SIZE = 200 * 1024 * 1024  # 200 MB
    MAX_BATCH_SIZE = 1000 * 1024 * 1024  # 1000 MB total per batch

    ALLOWED_EXTENSIONS = {'.pdf', '.docx', '.xlsx', '.xls'}
    ALLOWED_MIME_TYPES = {
        'application/pdf',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'application/vnd.ms-excel'
    }

    @classmethod
    def validate_file(cls, file_path: str, filename: str) -> Tuple[bool, Optional[str]]:
        """
        Validate a file for security and correctness.

        Args:
            file_path: Path to the uploaded file
            filename: Original filename

        Returns:
            (is_valid, error_message)
        """
        # Check extension
        _, ext = os.path.splitext(filename.lower())
        if ext not in cls.ALLOWED_EXTENSIONS:
            return False, f"File type '{ext}' not allowed. Allowed types: {', '.join(cls.ALLOWED_EXTENSIONS)}"

        # Check file size
        if not os.path.exists(file_path):
            return False, "File not found"

        file_size = os.path.getsize(file_path)
        if file_size > cls.MAX_FILE_SIZE:
            return False, f"File too large. Maximum size: {cls.MAX_FILE_SIZE // (1024*1024)} MB"

        if file_size == 0:
            return False, "File is empty"

        # Deep file type validation using magic bytes
        try:
            # Try to use python-magic if available
            import magic
            mime = magic.from_file(file_path, mime=True)
            if mime not in cls.ALLOWED_MIME_TYPES:
                return False, f"Invalid file type detected: {mime}. File extension doesn't match content."
        except (ImportError, AttributeError):
            # python-magic not available, skip deep validation
            pass

        # PDF-specific validation
        if ext == '.pdf':
            try:
                with open(file_path, 'rb') as f:
                    header = f.read(5)
                    if header != b'%PDF-':
                        return False, "File does not appear to be a valid PDF"
            except Exception as e:
                return False, f"Error validating PDF: {str(e)}"

        # DOCX and XLSX specific validation (both are ZIP archives)
        if ext in ['.docx', '.xlsx']:
            try:
                # DOCX/XLSX files are ZIP archives
                with open(file_path, 'rb') as f:
                    header = f.read(4)
                    if header != b'PK\x03\x04':
                        return False, f"File does not appear to be a valid {ext.upper()}"
            except Exception as e:
                return False, f"Error validating {ext.upper()}: {str(e)}"
                
        # XLS specific validation (OLE2 format)
        if ext == '.xls':
            try:
                with open(file_path, 'rb') as f:
                    header = f.read(8)
                    # OLE2 signature
                    if header != b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
                        return False, "File does not appear to be a valid XLS"
            except Exception as e:
                return False, f"Error validating XLS: {str(e)}"

        return True, None

    @classmethod
    def validate_batch(cls, files) -> Tuple[bool, Optional[str]]:
        """
        Validate a batch of files.

        Args:
            files: List of FileStorage objects

        Returns:
            (is_valid, error_message)
        """
        if not files:
            return False, "No files provided"

        if len(files) > 50:
            return False, "Too many files in batch. Maximum: 50 files"

        total_size = sum(len(f.read()) for f in files)
        # Reset file pointers
        for f in files:
            f.seek(0)

        if total_size > cls.MAX_BATCH_SIZE:
            return False, f"Batch too large. Maximum total size: {cls.MAX_BATCH_SIZE // (1024*1024)} MB"

        return True, None


def sanitize_filename(filename: str) -> str:
    """
    Sanitize a filename to prevent path traversal and other attacks.

    Args:
        filename: Original filename

    Returns:
        Sanitized filename
    """
    # Remove any directory components
    filename = os.path.basename(filename)

    # Remove or replace dangerous characters
    filename = re.sub(r'[^\w\s.-]', '', filename)

    # Remove leading/trailing spaces and dots
    filename = filename.strip('. ')

    # Limit length
    name, ext = os.path.splitext(filename)
    if len(name) > 200:
        name = name[:200]

    return name + ext


def check_default_credentials(username: str, password: str) -> bool:
    """
    Check if the user is trying to use default credentials.

    Returns:
        True if default credentials detected
    """
    default_combos = [
        ('admin', 'admin123'),
        ('admin', 'admin'),
        ('user', 'user'),
        ('test', 'test'),
    ]

    return (username.lower(), password) in default_combos
