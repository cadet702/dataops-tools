#!bin/env python3

import re
import os
import json
import yaml
import sys
from pathlib import Path
from collections import OrderedDict
import fire

from slalom.dataops import env, io, jobs
from slalom.dataops.logs import logged, logged_block, get_logger

IMAGE_BASE = "slalomggp/singer"
SINGER_PLUGINS_INDEX = os.environ.get("SINGER_PLUGINS_INDEX", "./singer_index.yml")

logging = get_logger("slalom.dataops.taputils")

try:
    import docker
    from slalom.dataops import dockerutils
except Exception as ex:
    docker = None
    dockerutils = None
    logging.warning("Docker libraries were not able to be loaded ({ex}).")


# ROOT_DIR = "."
_ROOT_DIR = "/projects/my-project"
VENV_ROOT = "/venv"
INSTALL_ROOT = "/usr/bin"
BASE_DOCKER_IMAGE = "slalomggp/singer"


def _get_root_dir():
    return "."


def _get_secrets_dir():
    result = os.environ.get("TAP_SECRETS_DIR", f"{_get_root_dir()}/.secrets")
    io.create_folder(result)
    return result


def _get_scratch_dir():
    result = os.environ.get("TAP_SCRATCH_DIR", f"{_get_root_dir()}/.output")
    io.create_folder(result)
    return result


def _get_root_dir():
    return _ROOT_DIR


def _get_secrets_dir():
    return os.environ.get("TAP_SECRETS_DIR", f"{_get_root_dir()}/.secrets")


def _get_scratch_dir():
    return os.environ.get("TAP_SCRATCH_DIR", f"{_get_root_dir()}/.output")


def _get_taps_dir(override=None):
    taps_dir = override or os.environ.get("TAP_CONFIG_DIR", ".")
    return io.make_local(taps_dir)  # if remote path provided, download locally


def _get_catalog_output_dir(tap_name):
    result = f"{_get_scratch_dir()}/taps/{tap_name}-catalog"
    io.create_folder(result)
    return result


def _get_plan_file(tap_name, taps_dir=None):
    return os.path.join(_get_taps_dir(taps_dir), f"data-plan-{tap_name}.yml")


def _get_select_file(taps_dir=None):
    return os.path.join(_get_taps_dir(taps_dir), f"data.select")


def _get_config_file(plugin_name, config_dir=None):
    """
    Returns a path to the configuration file which also contains secrets.
     - If file is blank or does not exist at the default secrets path, a new file will be created.
     - If any environment variables exist in the form of TAP_MY_TAP_my_setting, a new file
    will be created which contains these settings.
     - If the default file exists and environment variables also exist, the temp file will
    contain the default file values along with the environment variable overrides.
    """
    secrets_path = config_dir or _get_secrets_dir()
    default_path = f"{secrets_path}/{plugin_name}-config.json"
    tmp_path = f"{secrets_path}/tmp/{plugin_name}-config.json"
    use_tmp_file = False
    if io.file_exists(default_path):
        json_text = io.get_text_file_contents(default_path)
        conf_dict = json.loads(json_text)
    else:
        conf_dict = {}
        use_tmp_file = True
    for k, v in os.environ.items():
        prefix = f"{plugin_name.replace('-', '_').upper()}_"
        if k.startswith(prefix):
            setting_name = k.split(prefix)[1]
            conf_dict[setting_name] = v
            use_tmp_file = True
    if use_tmp_file:
        io.create_folder(str(Path(tmp_path).parent))
        io.create_text_file(tmp_path, json.dumps(conf_dict))
        return tmp_path
    else:
        return default_path


@logged(
    "installing '{plugin_name}' as '{alias or plugin_name}' "
    "using 'pip3 install {source or plugin_name}'"
)
def install(plugin_name, source=None, alias=None):
    source = source or plugin_name
    alias = alias or plugin_name

    venv_dir = os.path.join(VENV_ROOT, alias)
    install_path = os.path.join(INSTALL_ROOT, alias)
    if io.file_exists(install_path):
        response = input(
            f"The file '{install_path}' already exists. "
            f"Are you sure you want to replace this file? [y/n]"
        )
        if not response.lower() in ["y", "yes"]:
            raise RuntimeError(f"File already exists '{install_path}'.")
        io.delete_file(install_path)
    jobs.run_command(f"python3 -m venv {venv_dir}")
    jobs.run_command(f"{os.path.join(venv_dir ,'bin', 'pip3')} install {source}")
    jobs.run_command(f"ln -s {venv_dir}/bin/{plugin_name} {install_path}")


@logged("running discovery on '{tap_name}'")
def discover(tap_name, config_file=None, catalog_dir=None):
    if env.is_windows() or env.is_mac():
        _rerun_dockerized(tap_name)
        return
    config_file = config_file or _get_config_file(f"tap-{tap_name}")
    catalog_dir = catalog_dir or _get_catalog_output_dir(tap_name)
    catalog_file = f"{catalog_dir}/{tap_name}-catalog-raw.json"
    io.create_folder(catalog_dir)
    jobs.run_command(f"tap-{tap_name} --config {config_file} --discover > {catalog_file}")


@logged("Updating plan file for 'tap-{tap_name}'")
def plan(tap_name, taps_dir=None, config_file=None, config_dir=None, rescan=None):
    """
    Perform all actions necessary to prepare (plan) for a tap execution:
     1. Scan (discover) the source system metadata (if catalog missing or `rescan=True`)
     2. Apply filter rules from `select_file` and create human-readable `plan.yml` file to
        describe planned inclusions/exclusions.
     2. Create a new `catalog-selected.json` file which applies the plan file and which
        can be used by the tap to run data extractions.
    """
    if env.is_windows() or env.is_mac():
        _rerun_dockerized(tap_name)
        return
    taps_dir = _get_taps_dir(taps_dir)
    config_file = config_file or _get_config_file(
        f"tap-{tap_name}", config_dir=config_dir
    )
    catalog_dir = _get_catalog_output_dir(tap_name)
    catalog_file = f"{catalog_dir}/{tap_name}-catalog-raw.json"
    selected_catalog_file = f"{catalog_dir}/{tap_name}-catalog-selected.json"
    plan_file = _get_plan_file(tap_name, taps_dir)
    if (
        io.file_exists(catalog_file)
        and io.get_text_file_contents(catalog_file).strip() == ""
    ):
        logging.info(f"Cleaning up empty catalog file: {catalog_file}")
        io.delete_file(catalog_file)
    if rescan or not io.file_exists(catalog_file):
        discover(tap_name, config_file, catalog_dir)
    select_file = _get_select_file(taps_dir)
    select_rules = [
        line.split("#")[0].rstrip()
        for line in io.get_text_file_contents(select_file).splitlines()
        if line.split("#")[0].rstrip()
    ]
    matches = {}
    excluded_tables = []
    for table_object in _get_catalog_table_objects(catalog_file):
        table_name = table_object["stream"]
        table_match_text = f"{tap_name}.{table_name}"
        if _table_match_check(table_match_text, select_rules):
            matches[table_name] = {}
            for col_object in _get_catalog_table_columns(table_object):
                col_name = col_object
                col_match_text = f"{tap_name}.{table_name}.{col_name}"
                matches[table_name][col_name] = _col_match_check(
                    col_match_text, select_rules
                )
        else:
            excluded_tables.append(table_name)
    sorted_tables = sorted(matches.keys())
    file_text = ""
    file_text += f"selected_tables:\n"
    for table in sorted_tables:
        included_cols = [col for col, selected in matches[table].items() if selected]
        ignored_cols = [col for col, selected in matches[table].items() if not selected]
        file_text += f"{'  ' * 1}{table}:\n"
        file_text += f"{'  ' * 2}selected_columns:\n"
        for col in included_cols:
            file_text += f"{'  ' * 2}- {col}\n"
        if ignored_cols:
            file_text += f"{'  ' * 2}ignored_columns:\n"
            for col in ignored_cols:
                file_text += f"{'  ' * 2}- {col}\n"
    if excluded_tables:
        file_text += f"ignored_tables:\n"
        for table in sorted(excluded_tables):
            file_text += f"{'  ' * 1}- {table}\n"
    io.create_text_file(plan_file, file_text)
    _create_selected_catalog(
        tap_name,
        plan_file=plan_file,
        full_catalog_file=catalog_file,
        output_file=selected_catalog_file,
    )


@logged("syncing '{table_name or 'all tables'}' from '{tap_name}' to '{target_name}'")
def sync(
    tap_name,
    target_name="csv",
    table_name="*",
    taps_dir=None,
    *,
    config_file=None,
    config_dir=None,
    catalog_dir=None,
    target_config_file=None,
    rescan=False,
    state_file=None,
    dockerized: bool = None,
):
    """ Run a tap sync. If table_name is omitted, all sources will be extracted. """
    if dockerized is None:
        if env.is_windows() or env.is_mac():
            logging.info(
                "The 'dockerized' argument is not set when running either Windows or OSX."
                "Attempting to run sync from inside docker."
            )
            _rerun_dockerized(tap_name, target_name)
            return
    taps_dir = _get_taps_dir(taps_dir)
    select_file = _get_select_file(taps_dir)
    config_file = config_file or _get_config_file(f"tap-{tap_name}", config_dir)
    target_config_file = target_config_file or _get_config_file(
        f"target-{target_name}", config_dir
    )
    catalog_dir = catalog_dir or _get_catalog_output_dir(tap_name)
    full_catalog_file = f"{catalog_dir}/{tap_name}-catalog-selected.json"
    if rescan or select_file or not io.file_exists(full_catalog_file):
        plan(
            tap_name,
            taps_dir=taps_dir,
            config_file=config_file,
            config_dir=catalog_dir,
            rescan=rescan,
        )
    if table_name and table_name != "*":
        catalog_file = f"{catalog_dir}/{tap_name}-{table_name}-catalog.json"
        state_file = state_file or f"{catalog_dir}/{table_name}-state.json"
        _create_single_table_catalog(
            tap_name=tap_name,
            table_name=table_name,
            full_catalog_file=full_catalog_file,
            output_file=catalog_file,
        )
    else:
        catalog_file = full_catalog_file
        state_file = state_file or f"{catalog_dir}/full-extract-state.json"
    sync_cmd = (
        f"tap-{tap_name} --config {config_file} --catalog {catalog_file} | "
        f"target-{target_name} --config {target_config_file} "
        f">> {state_file}"
    )
    jobs.run_command(sync_cmd)
    # # tail -1 state.json > state.json.tmp && mv state.json.tmp state.json


def _get_catalog_table_objects(catalog_file):
    catalog_full = json.loads(Path(catalog_file).read_text())
    table_objects = catalog_full["streams"]
    return sorted(table_objects, key=lambda x: x["stream"])


def _get_catalog_table_columns(table_object):
    return table_object["schema"]["properties"].keys()


def _table_match_check(match_text: str, select_rules: list):
    selected = False
    for rule in select_rules:
        result = _check_table_rule(match_text, rule)
        if result == True:
            selected = True
        elif result == False:
            selected = False
    return selected


def _col_match_check(match_text: str, select_rules: list):
    selected = False
    for rule in select_rules:
        result = _check_column_rule(match_text, rule)
        if result == True:
            selected = True
        elif result == False:
            selected = False
    return selected


def _is_match(value, pattern):
    if not pattern:
        return None
    if value.lower() == pattern.lower():
        return True
    if pattern == "*":
        return True
    re_pattern = None
    if "/" in pattern:
        if pattern[0] == "/" and pattern[-1] == "/":
            re_pattern = pattern[1:-1]
            re_pattern = f"\\b{re_pattern}\\b"
            # logging.info(f"Found regex pattern: {pattern}")
    elif "*" in pattern:
        # logging.info(f"Found wildcard pattern: {pattern}")
        re_pattern = pattern.replace("*", ".*")
    if re_pattern:
        # logging.info(f"Checking regex pattern: {re_pattern}")
        result = re.search(re_pattern.lower(), value.lower())
        if result:
            # logging.info(f"Matched regex pattern '{re_pattern}' on '{value}'")
            return True
    return False


# @logged("checking '{match_text}' against rule '{rule_text}'")
def _check_column_rule(match_text: str, rule_text: str):
    """ Checks rule. Returns True to include, False to exclude, or None if not a match """
    if rule_text[0] == "!":
        match_result = False  # Exclude if matched
        rule_text = rule_text[1:]
    else:
        match_result = True  # Include if matched
    rule_text = rule_text.replace("**.", "*.*.")
    tap_match, table_match, column_match = (
        rule_text.split(".")[0],
        rule_text.split(".")[1],
        ".".join(rule_text.split(".")[2:]),
    )
    if not _is_match(match_text.split(".")[0], tap_match):
        return None
    if not _is_match(match_text.split(".")[1], table_match):
        return None
    if not _is_match(match_text.split(".")[2], column_match):
        return None
    # logging.info(
    #     f"Column '{match_text}' matched column filter '{column_match}' in '{rule_text}'"
    # )
    return match_result


def _check_table_rule(match_text: str, rule_text: str):
    """ Checks rule. Returns True to include, False to exclude, or None if not a match """
    if rule_text[0] == "!":
        match_result = False  # Exclude if matched
        rule_text = rule_text[1:]
    else:
        match_result = True  # Include if matched
    rule_text = rule_text.replace("**.", "*.*.")
    if "*.*." in rule_text:
        return None  # Global column rules do not apply to tables
    if match_result == True and rule_text[-2:] == ".*":
        rule_text = rule_text[:-2]
    if len(rule_text.split(".")) > 2:
        return None  # Column rules do not apply to tables
    tap_name, table_name = (match_text.split(".")[0], ".".join(match_text.split(".")[1:]))
    tap_rule, table_rule = (rule_text.split(".")[0], ".".join(rule_text.split(".")[1:]))
    if not _is_match(tap_name, tap_rule):
        return None
    if not _is_match(table_name, table_rule):
        return None
    # logging.info(
    #     f"Table '{match_text}' matched table filter '{table_rule}' in '{rule_text}'"
    # )
    return match_result


@logged(
    "selecting catalog metadata "
    "from '{tap_name}' source catalog file: {full_catalog_file}"
)
def _create_selected_catalog(
    tap_name, plan_file=None, full_catalog_file=None, output_file=None
):
    catalog_dir = _get_catalog_output_dir(tap_name)
    source_catalog_path = full_catalog_file or os.path.join(
        catalog_dir, "catalog-raw.json"
    )
    output_file = output_file or os.path.join(catalog_dir, f"selected-catalog.json")
    catalog_full = json.loads(Path(source_catalog_path).read_text())
    full_table_list = sorted([tbl["stream"] for tbl in catalog_full["streams"]])
    plan_file = plan_file or _get_plan_file(tap_name)
    plan = yaml.safe_load(io.get_text_file_contents(plan_file))
    included_table_objects = []
    for tbl in catalog_full["streams"]:
        stream_name = tbl["stream"]
        if stream_name in plan["selected_tables"].keys():
            _select_table(tbl)
            for col_name in _get_catalog_table_columns(tbl):
                col_selected = (
                    col_name in plan["selected_tables"][stream_name]["selected_columns"]
                )
                _select_table_column(tbl, col_name, col_selected)
            included_table_objects.append(tbl)
    catalog_new = {"streams": included_table_objects}
    with open(output_file, "w") as f:
        json.dump(catalog_new, f, indent=2)


def _select_table(tbl: object):
    for metadata in tbl["metadata"]:
        if len(metadata["breadcrumb"]) == 0:
            metadata["metadata"]["selected"] = True


def _select_table_column(tbl: object, col_name: str, selected: bool):
    for metadata in tbl["metadata"]:
        if (
            len(metadata["breadcrumb"]) >= 2
            and metadata["breadcrumb"][0] == "properties"
            and metadata["breadcrumb"][1] == col_name
        ):
            metadata["metadata"]["selected"] = selected
            return
    tbl["metadata"].append(
        {"breadcrumb": ["properties", col_name], "metadata": {"selected": selected}}
    )
    return


@logged(
    "selecting '{table_name}' catalog metadata "
    "from '{tap_name}' source catalog file: {full_catalog_file}"
)
def _create_single_table_catalog(
    tap_name, table_name, full_catalog_file=None, output_file=None
):
    catalog_dir = _get_catalog_output_dir(tap_name)
    source_catalog_path = full_catalog_file or os.path.join(
        catalog_dir, "catalog-selected.json"
    )
    output_file = output_file or os.path.join(catalog_dir, f"{table_name}-catalog.json")
    included_table_objects = []
    catalog_full = json.loads(Path(source_catalog_path).read_text())
    full_table_list = sorted([tbl["stream"] for tbl in catalog_full["streams"]])
    for tbl in catalog_full["streams"]:
        stream_name = tbl["stream"]
        if stream_name == table_name:
            for metadata in tbl["metadata"]:
                if len(metadata["breadcrumb"]) == 0:
                    metadata["metadata"]["selected"] = True
            included_table_objects.append(tbl)
    catalog_new = {"streams": included_table_objects}
    with open(output_file, "w") as f:
        json.dump(catalog_new, f, indent=2)


def _get_docker_tap_image(tap_alias, target_alias=None):
    if tap_alias.startswith("tap-"):
        tap_alias = tap_alias.replace("tap-", "")
    if not target_alias:
        return f"{BASE_DOCKER_IMAGE}:tap-{tap_alias}"
    else:
        if target_alias.startswith("target-"):
            target_alias = target_alias.replace("target-", "")
        return f"{BASE_DOCKER_IMAGE}:{tap_alias}-to-{target_alias}"


def _rerun_dockerized(tap_alias, target_alias=None):
    cmd = f"s-tap {' '.join(sys.argv[1:])}"
    env = {
        k: v
        for k, v in os.environ.items()
        if (k.startswith("TAP_") or k.startswith("TARGET_"))
    }
    docker_client = docker.from_env()
    image_name = _get_docker_tap_image(tap_alias, target_alias)
    try:
        dockerutils.pull(image_name)
    except Exception as ex:
        logging.warning(f"Could not pull latest Spark image '{image_name}'. {ex}")
    with logged_block(f"running dockerized command '{cmd}' on image '{image_name}'"):

        def _build_docker_run(image, command, environment, working_dir, volumes):
            e_str = " ".join([f"-e {k}={v}" for k, v in environment.items()])
            w_str = f"-w {working_dir}" if working_dir else ""
            v_str = " ".join([f"-v {x}:{y}" for x, y in volumes.items()])
            docker_run_cmd = f"docker run {e_str} {v_str} {w_str} {image} {command}"
            return docker_run_cmd

        volumes = {os.path.abspath("."): "/projects/my-project"}
        DEBUG = True
        if DEBUG:
            container_lib = "/usr/local/lib/python3.8/site-packages/slalom"
            host_lib = "C:\Files\Source\dataops-tools\slalom"
            volumes[host_lib] = container_lib
        docker_run_cmd = _build_docker_run(
            image=image_name,
            command=cmd,
            environment=env,
            working_dir="/projects/my-project",
            volumes=volumes,
        )
        return_code, output_text = jobs.run_command(docker_run_cmd)
    return True


def build_image(tap_or_plugin_alias, target_alias=None, push=False, pre=False):
    name, source, alias = _get_plugin_info(f"tap-{tap_or_plugin_alias}")
    _build_plugin_image(name, source=source, alias=alias, push=push, pre=pre)
    if target_alias:
        name, source, alias = _get_plugin_info(f"target-{target_alias}")
        _build_plugin_image(name, source=source, alias=alias, push=push, pre=pre)
        _build_composite_image(
            tap_alias=tap_or_plugin_alias, target_alias=target_alias, push=push, pre=pre
        )


def _get_plugins_list(plugins_index=None):
    plugins_index = plugins_index or SINGER_PLUGINS_INDEX
    if not io.file_exists(plugins_index):
        raise RuntimeError(
            f"No file found at '{plugins_index}'."
            "Please set SINGER_PLUGINS_INDEX and try again."
        )
    yml_doc = yaml.safe_load(io.get_text_file_contents(plugins_index))
    taps = yml_doc["singer-taps"]
    list_of_tuples = []
    taps = yml_doc["singer-taps"]
    targets = yml_doc["singer-targets"]
    plugins = taps + targets
    for plugin in plugins:
        list_of_tuples.append(
            (plugin["name"], plugin.get("source", None), plugin.get("alias", None))
        )
    return list_of_tuples


def _build_all_standalone(source_image=None, plugins_index=None, push=False, pre=False):
    plugins = _get_plugins_list(plugins_index)
    for name, source, alias in plugins:
        image_name = _build_plugin_image(
            name,
            source=source,
            alias=alias,
            source_image=source_image,
            push=push,
            pre=pre,
        )


def _get_plugin_info(id, plugins_index=None):
    plugins = _get_plugins_list(plugins_index)
    for name, source, alias in plugins:
        if (alias or name) == id:
            return (name, source, alias)
    raise ValueError(f"Could not file a plugin called '{id}'")


def _build_all_composite(source_image=None, plugins_index=None, push=False, pre=False):
    plugins = _get_plugins_list(plugins_index)
    for tap_name, tap_source, tap_alias in plugins:
        tap_alias = tap_alias or tap_name
        for target_name, target_source, target_alias in plugins:
            target_alias = target_alias or target_name
            if tap_alias.startswith("tap-") and target_alias.startswith("target-"):
                image_name = _build_composite_image(
                    tap_alias, target_alias, push=push, pre=pre
                )


def _build_plugin_image(
    plugin_name, source, alias, source_image=None, push=False, pre=False
):
    source = source or plugin_name
    alias = alias or plugin_name
    image_name = f"{IMAGE_BASE}:{alias}"
    build_cmd = f"docker build"
    if source_image:
        build_cmd += f" --build-arg source_image={source_image}"
    if pre:
        build_cmd += f" --build-arg prerelease=true"
        image_name += "--pre"
    build_cmd += (
        f" --build-arg PLUGIN_NAME={plugin_name}"
        f" --build-arg PLUGIN_SOURCE={source}"
        f" --build-arg PLUGIN_ALIAS={alias}"
        f" -t {image_name}"
        f" -f plugin.Dockerfile"
        f" ."
    )
    jobs.run_command(build_cmd)
    if push:
        _push(image_name)
    return image_name


def _build_composite_image(tap_alias, target_alias, push=False, pre=False):
    if tap_alias.startswith("tap-"):
        tap_alias = tap_alias.replace("tap-", "", 1)
    if target_alias.startswith("target-"):
        target_alias = target_alias.replace("target-", "", 1)
    image_name = f"{IMAGE_BASE}:{tap_alias}-to-{target_alias}"
    build_cmd = f"docker build"
    if pre:
        build_cmd += " --build-arg source_image_suffix=--pre"
        image_name += "--pre"
    build_cmd += (
        f" --build-arg tap_alias={tap_alias}"
        f" --build-arg target_alias={target_alias}"
        f" -t {image_name}"
        f" -f tap-to-target.Dockerfile"
        f" ."
    )
    jobs.run_command(build_cmd)
    if push:
        _push(image_name)
    return image_name


def _push(image_name):
    jobs.run_command(f"docker push {image_name}")


def build_all_images(push=False, pre=False):
    """
    Build all images.
    :param push: Push images after building
    :param pre: Create and publish pre-release builds
    """
    _build_all_standalone(push=push, pre=pre)
    _build_all_composite(push=push, pre=pre)


def main():
    fire.Fire(
        {
            "install": install,
            "discover": discover,
            "plan": plan,
            "sync": sync,
            "build_image": build_image,
            "build_all_images": build_all_images,
        }
    )


if __name__ == "__main__":
    main()
