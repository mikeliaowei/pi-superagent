

def toml_path(path):
    """A path for a TOML basic string: Windows backslashes would read as escapes (\\U...), forward slashes work everywhere."""
    return str(path).replace('\\', '/')
