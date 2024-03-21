import json
from natsort import natsorted
from sqlalchemy import (
    MetaData,
    Table,
    Integer,
    String,
    func,
    Float,
)
from sqlalchemy.exc import DataError, IntegrityError, ProgrammingError
import pandas as pd
import numpy as np
import signal
import sys
import os
from datetime import datetime
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv
from django.conf import settings

from .import_utils import *


load_dotenv()

# get command line arguments
rootDir = os.environ.get("PIPELINE_OUTPUT_PATH") or os.path.join(settings.BASE_DIR, 'data/fixtures')
chunk_size = int(os.environ.get("CHUNK_SIZE"))
#verbose = os.environ.get("VERBOSE") == "true"
dbConnectionString = os.environ.get("DB")
copy_maps_from_job = os.environ.get("COPY_MAPS_FROM_JOB")
isDevelopment = os.environ.get("ENVIRONMENT") != "production"
schema = os.environ.get("SCHEMA_NAME")
start_at_model = os.environ.get("START_AT_MODEL") if os.environ.get("START_AT_MODEL") != "" else None
start_at_file = os.environ.get("START_AT_FILE") if os.environ.get("START_AT_FILE") != "" else None

if rootDir == None:
    print("No root directory specified")
    exit()

data_issue_logger = None
output_logger = None

print(rootDir)

engine = None

metadata = MetaData()

job_dir = ""
maps_load_dir = ""
# map of import functions
model_import_actions = {
    "genes": {
        "name": "genes",
        "pk_lookup_col": "short_name",
        "fk_map": {},
        "filters": {
            "short_name": lambda x: x.upper() if x is not None else None
        }
    },
    "transcripts": {
        "name": "transcripts",
        "pk_lookup_col": "transcript_id",
        "fk_map": {"gene": "genes"}
    },
    "variants": {
        "name": "variants",
        "pk_lookup_col": "variant_id",
        "fk_map": {}
    },
    "variants_transcripts": {
        "name": "variants_transcripts",
        "pk_lookup_col": ["transcript", "variant"],
        "fk_map": {"transcript": "transcripts", "variant": "variants"}
    },
    "variants_annotations": {
        "name": "variants_annotations",
        "pk_lookup_col": None,
        "fk_map": {"DO_COMPOUND_FK": "for variants_transcripts"},
        "filters":{
            "hgvsp": lambda x: x.replace("%3D","=") if x is not None else None
        }
    },
    "severities":{
        "name":"severities",
        "pk_lookup_col": None,
        "fk_map": {},
        },
    "variants_consequences": {
        "name": "variants_consequences",
        "pk_lookup_col": None,
        "fk_map": {"DO_COMPOUND_FK": "for variants_transcripts"}
    },
    "sv_consequences": {
        "name": "sv_consequences",
        "pk_lookup_col": None,
        "fk_map": {"gene": "genes", "variant": "variants"}
    },
    "snvs": {
        "name": "snvs",
        "pk_lookup_col": None,
        "fk_map": {"variant": "variants"},
        "filters":{
            "dbsnp_id": lambda x: x.split('&')[0] if x is not None else None
        }
    },
    "svs": {
        "name": "svs",
        "pk_lookup_col": None,
        "fk_map": {"variant": "variants"}
    },
    "svs_ctx": {
        "name": "svs_ctx",
        "pk_lookup_col": None,
        "fk_map": {"variant": "variants"}
    },
    "str": {
        "name": "str",
        "pk_lookup_col": None,
        "fk_map": {"variant": "variants"}
    },
    "mts": {
        "name": "mts",
        "pk_lookup_col": None,
        "fk_map": {"variant": "variants"}
    },
    "genomic_ibvl_frequencies": {
        "name": "genomic_ibvl_frequencies",
        "pk_lookup_col": None,
        "fk_map": {"variant": "variants"}
    },
    "genomic_gnomad_frequencies": {
        "name": "genomic_gnomad_frequencies",
        "pk_lookup_col": None,
        "fk_map": {"variant": "variants"}
    },
    "mt_ibvl_frequencies": {
        "name": "mt_ibvl_frequencies",
        "pk_lookup_col": None,
        "fk_map": {"variant": "variants"}
    },
    "mt_gnomad_frequencies": {
        "name": "mt_gnomad_frequencies",
        "pk_lookup_col": None,
        "fk_map": {"variant": "variants"}
    },
}


pk_maps = {}
next_id_maps = {}
tables = {}

def load_maps(models=[]):
    try:
        # Load from files
        for modelName in models:
            with open(maps_load_dir + "/" + modelName + "_pk_map.json", "r") as f:
                pk_maps[modelName] = json.load(f)
            with open(maps_load_dir + "/" + modelName + "_next_id.json", "r") as f:
                next_id_maps[modelName] = json.load(f)
            log_output("loaded map for " + modelName +". number of records: " + str(len(pk_maps[modelName])))

    except FileNotFoundError:
        pk_maps[modelName] = {}
        pass

def append_to_map(modelName, key, value):
    if modelName not in pk_maps:
        log_output("")
        load_maps(models=[modelName])
    if key not in pk_maps[modelName]:
        pk_maps[modelName][key] = value

def persist_and_unload_maps():
    try:
        for modelName, pk_map in pk_maps.items():
            log_output("saving pk map for " + modelName)
            with open(os.path.join(job_dir,modelName+"_pk_map.json"), "w") as f:
                json.dump(pk_map, f)
        for modelName, next_id in next_id_maps.items():
            with open(os.path.join(job_dir, modelName+"_next_id.json"), "w") as f:
                json.dump(next_id, f)
    except Exception as e:
        log_data_issue("Error saving maps")
        quit()
    pk_maps.clear()
    log_output("cleared the pk maps")

def resolve_PK(referencedModel, name):
    if name is None:
        return None
    try:
        result = pk_maps[referencedModel][name.upper()]
        return result
    except KeyError:
        return None

def get_table(model):
    global tables
    if model in tables:
        return tables[model]
    else:
        if isinstance(schema, str) and len(schema) > 0:
            table = Table(model, metadata, schema=schema)
        else:
            table = Table(model, metadata, autoload_with=engine)
        tables[model] = table
        return table
    
def inject(model, data, map_key):
# need to dynamically inject the single obj that was missing from original data
    pk = None
    table = get_table(model)
    with engine.connect() as connection:
        try:
            id = next_id_maps[model]
            data["id"] = id
            connection.execute(table.insert(), data)
            connection.commit()
#            pk = result.inserted_primary_key[0]
            append_to_map(model, map_key, id)
            next_id_maps[model]  = id + 1
            log_data_issue(f"dynamically added to {model}: {data}")
            pk = id
        except IntegrityError as e:
            log_data_issue("a dynamically injected obj had an integrity error.")
            log_data_issue(e)
#                                quit() # LATER: comment this out
        except Exception as e:
            log_data_issue("a dynamically injected obj had an error.")
            log_data_issue(e)
#                                quit() # LATER: comment this out?
    return pk

def import_file(file, file_info, action_info):
    name = action_info.get("name")
    fk_map = action_info.get("fk_map")
    pk_lookup_col = action_info.get("pk_lookup_col")
    filters = action_info.get("filters") or {}

    missingRefCount = 0
    table = get_table(name)
    types_dict = {}
    for column in table.columns:
        # convert sql types to pandas types
        if isinstance(column.type, Integer):
            types_dict[column.name] = "Int64"
        elif isinstance(column.type, String):
            types_dict[column.name] = "str"
        elif isinstance(column.type, Float):
            types_dict[column.name] = "float64"
        else:
            pass

    df = readTSV(file, file_info, dtype=types_dict)
    df.replace(np.nan, None, inplace=True)
    
    data_list = []
    for index, row in df.iterrows():
        data = row.to_dict()
        pk = next_id_maps[name] + index
        data["id"] = pk

        skip = False
        for col, filter in filters.items():
            data[col] = filter(data[col])
        for fk_col, fk_model in fk_map.items():
            map_key = None
            resolved_pk = None
            debug_row = None
            if fk_col == "DO_COMPOUND_FK":

                debug_row = data.copy()
                v_id = resolve_PK("variants", data["variant"])
                t_id = resolve_PK("transcripts", data["transcript"])
                map_key = "-".join([str(t_id), str(v_id)])
                del data["variant"]
                del data["transcript"]
                resolved_pk = resolve_PK("variants_transcripts", map_key)
                fk_col = "variant_transcript"
            else:
                if isinstance(data[fk_col], str):
                    map_key = data[fk_col].upper()
                if map_key == "NA":
                    data[fk_col] = None
                else:
                    resolved_pk = resolve_PK(fk_model, map_key)
                    ## resolved PK was not found from maps, so.. if it's a gene, we could dynamically inject
                    if (resolved_pk == None and fk_col == "gene" and name == "transcripts"):
                        resolved_pk = inject("genes",{"short_name":map_key}, map_key)
                    elif (resolved_pk == None and fk_col == "variant" and name in ["sv_consequences", "svs", "snvs", "mts"]):
                        
                        if (name == "sv_consequences" or name == "svs"):
                            var_type = "SV"
                        elif (name == "snvs"):
                            var_type = "SNV"
                        elif (name == "mts"):
                            var_type = "MT"
                        resolved_pk = inject("variants",{"variant_id":map_key, "var_type": var_type}, map_key)
            if map_key is not None and False:
                log_output(
                    "resolved "
                    + fk_model
                    + "."
                    + data[fk_col]
                    + " to "
                    + str(resolved_pk)
                )
            if resolved_pk is not None:
                data[fk_col] = resolved_pk
            else:
                log_data_issue(
                    "Missing "
                    + fk_col if fk_col is not None else "None"
                    + " "
                    + map_key if map_key is not None else "None"
                    + " referenced from "
                    + name if name is not None else "None"
                )
                if (debug_row is not None):
                    log_data_issue(debug_row)
                else:
                    log_data_issue(data)
                missingRefCount += 1
                skip = True
        if skip:
            continue
        for col in data:
            if col in table.columns and isinstance(table.columns[col].type, String) and data[col] is None:
                data[col] = ""
#                print("replaced None with empty string. col: " + col + " value: " + str(data[col]))
        for table_col in table.columns:
            if table_col.name not in data:
                if isinstance(table_col.type, String):
                    data[table_col.name] = ""
#                    print("filled missing col " + table_col.name + " with empty string")
                else:
                    data[table_col.name] = None
#                    print("filled missing col " + table_col.name + " with None")
        
        data_list.append(data)

    # dispose of df to save ram
    del df
    with engine.connect() as connection:
        successCount = 0
        failCount = 0
        duplicateCount = 0
        successful_chunks = 0
        fail_chunks = 0

        for chunk in chunks(data_list, chunk_size):
            try:
                connection.execute(table.insert(), chunk)
                #commit
                connection.commit()
                # chunk worked
                successful_chunks += 1
                successCount += len(chunk)
            except Exception as e:
                #                print(e)
                connection.rollback()
                fail_chunks += 1
                for row in chunk:
                    did_succeed = False
                    try:
                        connection.execute(table.insert(), row)
                        connection.commit()
                        successCount += 1
                        did_succeed = True

                    except DataError as e:
                        log_data_issue(e)
                        failCount += 1
#                        quit()
                    except IntegrityError as e:
                        msg = str(e)
                        if "Duplicate" in msg or "ORA-00001" in msg:
                            duplicateCount += 1
                            successCount += 1
                        else:
                            failCount += 1
                            log_data_issue(e)
#                            quit()
                    except Exception as e:
                        
                        log_data_issue(e)
                        failCount += 1
                    if (not did_succeed):
                        connection.rollback()

            if pk_lookup_col is not None:
                pk_map = {}
                for data in chunk:
                    # record the PKS for each row that was added
                    if isinstance(pk_lookup_col, list):
#                        log_output(pk_lookup_col)
#                        log_output(data)
                        map_key = "-".join([str(data[col]) for col in pk_lookup_col])
                    elif isinstance(pk_lookup_col, str) and isinstance(data[pk_lookup_col], str):
                        map_key = data[pk_lookup_col]
                    
                    if map_key not in pk_map:
                        pk_map[map_key] = data["id"]
                    if False:
                        log_output("added " + name + "." + map_key + " to pk map")
                for key in pk_map:
                    append_to_map(name, key.upper(), pk_map[key])

    next_id_maps[name] += file_info["total_rows"]

    return {
        "success": successCount,
        "fail": failCount,
        "missingRef": missingRefCount,
        "duplicate": duplicateCount,
        "successful_chunks": successful_chunks,
        "fail_chunks": fail_chunks,
    }

def cleanup(sig, frame):
    global engine, pk_maps, next_id_maps, tables, metadata, data_issue_logger, output_logger
    print('cleaning up ...')
    persist_and_unload_maps()
    engine.dispose()
    #garbage collect
    del pk_maps
    del next_id_maps
    del tables
    del metadata
    del data_issue_logger
    del output_logger
    print('done')
    sys.exit(0)

signal.signal(signal.SIGINT, cleanup)

def start(db_engine):

    arrived_at_start_model = False
    arrived_at_start_file = False
    # Assuming 'engine' is your Engine object
    global job_dir, maps_load_dir, engine, schema
    engine = db_engine

    if isinstance(schema,str) and len(schema) > 0:
        metadata.reflect(bind=engine, schema=schema)
    else:
        metadata.reflect(bind=engine)
    Session = sessionmaker(bind=engine)
    jobs_dir = os.path.abspath(os.path.join("data/import_script", "jobs"))
    os.makedirs(jobs_dir, exist_ok=True)
    os.makedirs(os.path.join(jobs_dir, "1"), exist_ok=True)
    
    without_hidden = [f for f in os.listdir(jobs_dir) if not f.startswith('.')]
    last_job = int(natsorted(without_hidden)[-1])
    if (os.listdir(os.path.join(jobs_dir, str(last_job))) == []):
        job_dir = os.path.join(jobs_dir, str(last_job))
    else:
        job_dir = os.path.join(jobs_dir, str(last_job + 1))
    os.makedirs(job_dir, exist_ok=True)
    os.chmod(job_dir, 0o777)  # Set read and write permissions for the directory
    print("using job dir " + job_dir)
    setup_loggers(job_dir)

    if copy_maps_from_job is not None and copy_maps_from_job != "":
        maps_load_dir = os.path.join(jobs_dir, copy_maps_from_job)
    else:
        maps_load_dir = job_dir

    now = datetime.now()
    counts = {}
    counts["success"] = 0
    counts["fail"] = 0
    counts["missingRef"] = 0
    counts["duplicate"] = 0
    counts["successful_chunks"] = 0
    counts["fail_chunks"] = 0

    for modelName, action_info in model_import_actions.items():
        model_counts = {}
        model_counts["success"] = 0
        model_counts["fail"] = 0
        model_counts["missingRef"] = 0
        model_counts["duplicate"] = 0
        model_counts["successful_chunks"] = 0
        model_counts["fail_chunks"] = 0
        model_directory = rootDir + "/" + modelName


        if isinstance(start_at_model, str) and modelName != start_at_model and not arrived_at_start_model:
            log_output("Skipping " + modelName +", until "+start_at_model)
            continue

        if isinstance(start_at_model, str) and modelName == start_at_model:
            arrived_at_start_model = True

        if action_info.get("skip") or not os.path.isdir(model_directory):
            log_output("Skipping " + modelName + " (expected dir: " + model_directory + ")")
            continue

        referenced_models = action_info.get("fk_map").values()
        if "DO_COMPOUND_FK" in action_info.get("fk_map"):
            referenced_models = ["variants_transcripts", "variants", "transcripts"]
        load_maps(models=referenced_models)
        modelNow = datetime.now()
        
        # if modelName not in pk_maps:
        #     pk_maps[modelName] = {}
        if modelName not in next_id_maps:
            next_id_maps[modelName] = 1
        if action_info.get("empty_first") and isDevelopment and False:
            log_output("Emptying table " + modelName)
            table = get_table(modelName)


            # Assuming 'table' is your Table object
            # Replace 'ID' with your actual column name
            with Session() as session:
                max_id = session.query(func.max(table.columns['id'])).scalar()

            print("max id found to be "+str(max_id))
            with engine.connect() as connection:

                # Split the deletion into smaller chunks
                chunk_size = 1000
                offset = 0
                while True:
                    try:
                        delete_stmt = table.delete().where(table.c.id <= max_id - offset).where(table.c.id > max_id - chunk_size - offset)
                        connection.execute(delete_stmt)
                        offset += chunk_size
                        connection.commit()
#                        print("did delete a chunk")
                    except ProgrammingError as e:
                        print(e)
                        break
                    except Exception as e:
                        print("error emptying table " + modelName)
                        print(e)
                        break
        sorted_files = natsorted(
            [f for f in os.listdir(model_directory) if not f.startswith('.')],
        )

        for file in sorted_files:
            if file.endswith(".tsv"):

                if isinstance(start_at_file, str) and file != start_at_file and not arrived_at_start_file:
                    log_output("Skipping " + file +", until "+start_at_file)
                    continue
                if isinstance(start_at_file, str) and file == start_at_file:
                    arrived_at_start_file = True
                targetFile = model_directory + "/" + file
                file_info = inspectTSV(targetFile)
                log_output(
                    "\nimporting "
                    + modelName
                    + " ("
                    + targetFile.split("/")[-1]
                    + "). Expecting "
                    + str(file_info["total_rows"])
                    + " rows..."
                )
                # log_output(targetFile)
                if (file_info["total_rows"] == 0):
                    log_output("Skipping empty file")
                    continue
                results = import_file(
                    targetFile,
                    file_info,
                    action_info,
                )
                if results["success"] == 0:
                    log_output("No rows were imported.")
                    
                for key in ["success", "fail", "missingRef", "duplicate", "successful_chunks", "fail_chunks"]:
                    model_counts[key] += results[key]
                    counts[key] += results[key]

                report_counts(results)

        log_output(
            "\nFinished importing "
            + modelName
            + ". Took this much time: "
            + str(datetime.now() - modelNow)
        )
        report_counts(model_counts)
        this_model_index = list(model_import_actions.keys()).index(modelName)
        if this_model_index + 1 < len(model_import_actions.keys()):
            leftover_models = list(model_import_actions.keys())[this_model_index+1:]
            log_output("\nmodels left still: " + str(leftover_models) + "\n")
        
        persist_and_unload_maps()
    log_output("finished importing IBVL. Time Taken: " + str(datetime.now() - now))
    report_counts(counts)
    cleanup(None, None)
