import azure.functions as func
import logging


import os
import argparse
import glob
import html
import io
import re
import time
import json
from tenacity import retry, wait_random_exponential, stop_after_attempt  
from PyPDF2 import PdfReader, PdfWriter
from azure.identity import AzureDeveloperCliCredential
from azure.core.credentials import (
    AzureKeyCredential,
    AzureNamedKeyCredential,    
)
from azure.storage.filedatalake.aio import DataLakeServiceClient
from azure.storage.blob import BlobServiceClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import *
from azure.search.documents import SearchClient
from azure.ai.formrecognizer import DocumentAnalysisClient
import openai
from azure.search.documents.indexes.models import VectorSearchAlgorithmConfiguration

import argparse
import asyncio
from typing import Any, Optional, Union

from azure.core.credentials import AzureKeyCredential
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import AzureDeveloperCliCredential

from prepdocslib.blobmanager import BlobManager
from prepdocslib.embeddings import (
    AzureOpenAIEmbeddingService,
    OpenAIEmbeddings,
    OpenAIEmbeddingService,
)
from prepdocslib.filestrategy import DocumentAction, FileStrategy
from prepdocslib.listfilestrategy import (
    ADLSGen2ListFileStrategy,
    ADLSGen2SingleFileStrategy,
    ListFileStrategy,
    LocalListFileStrategy,
)
from prepdocslib.pdfparser import DocumentAnalysisPdfParser, LocalPdfParser, PdfParser
from prepdocslib.strategy import SearchInfo, Strategy
from prepdocslib.textsplitter import TextSplitter
from azure.storage.blob import BlobServiceClient





MAX_SECTION_LENGTH = os.environ["MAX_SECTION_LENGTH"] #1000
SENTENCE_SEARCH_LIMIT = os.environ["SENTENCE_SEARCH_LIMIT"] #100
SECTION_OVERLAP = os.environ["SECTION_OVERLAP"] #100
AZURE_OPENAI_SERVICE = os.getenv("AZURE_OPENAI_SERVICE")

localpdfparser = False
search_service_name = os.environ["AZURE_SEARCH_SERVICE"] #os.environ['az_search_service_name']
search_creds = os.environ["AZURE_SEARCH_KEY"] #os.environ['az_search_key']
search_index_name = os.environ["AZURE_SEARCH_INDEX"] #os.environ['az_search_index_name']
search_service_category = os.environ['AZURE_SEARCH_SERVICE_CATEGORY'] #os.environ['az_search_service_category'] #field name
inbound_doc_storage_account_connection_string = os.environ['AZURE_INBOUND_DOC_STORAGE_ACCOUNT_CONNECTION_STRING'] #os.environ['inbound_doc_storage_account_connection_string']
inbound_doc_storage_account_name = os.environ['AZURE_STORAGE_ACCOUNT'] #os.environ['outbound_doc_storage_account_name']
inbound_doc_storage_creds = os.environ['AZURE_OUTBOUND_DOC_STORAGE_ACCOUNT_KEY'] #os.environ['outbound_doc_storage_account_key']
inbound_doc_storage_account_container = os.environ['AZURE_INBOUND_STORAGE_CONTAINER'] #os.environ['outbound_doc_storage_account_container']
outbound_doc_storage_account_connection_string = os.environ['AZURE_OUTBOUND_DOC_STORAGE_ACCOUNT_CONNECTION_STRING']
outbound_doc_storage_account_name = os.environ['AZURE_STORAGE_ACCOUNT'] #os.environ['outbound_doc_storage_account_name']
oubdound_doc_storage_creds = os.environ['AZURE_OUTBOUND_DOC_STORAGE_ACCOUNT_KEY'] #os.environ['outbound_doc_storage_account_key']
outbound_doc_storage_account_container = os.environ['AZURE_STORAGE_CONTAINER'] #os.environ['outbound_doc_storage_account_container']
formrecognizerservice = os.environ['AZURE_FORMRECOGNIZER_SERVICE'] #os.environ['form_recognizer_service']
formrecognizer_creds = os.environ['AZURE_FORMRECOGNIZER_KEY'] #os.environ['form_recognizer_key']
search_analyzer_name = os.environ['AZURE_SEARCH_ANALYZER_NAME']
# Used by the OpenAI SDK
openai_gpt_model = os.environ["AZURE_OPENAI_CHATGPT_MODEL"] #os.environ['openai_gpt_model']
openai_emb_model = os.getenv("AZURE_OPENAI_EMB_MODEL_NAME", "text-embedding-ada-002") #os.environ['openai_emb_model']
openai.api_type =  "azure_ad" #os.environ['openai_api_type'] #azure
openai.api_base = f"https://{AZURE_OPENAI_SERVICE}.openai.azure.com" #f"https://{os.environ['openai_service_name']}.openai.azure.com"
openai.api_version = os.getenv("OPENAI_API_VERSION") #"2023-07-01-preview" #"2022-12-01"
openai.api_key = os.getenv("OPENAI_API_KEY") #os.environ['openai_api_key']


app = func.FunctionApp()

def log_configuration(sensitive = False):
    logging.info(f"search_service_name: {search_service_name}")
    if sensitive:
        logging.info(f"search_creds: {search_creds}")
    else:
        logging.info(f"search_creds: {search_creds[:4]}...")
    logging.info(f"search_index_name: {search_index_name}")
    logging.info(f"search_service_category: {search_service_category}")
    if sensitive:
        logging.info(f"inbound_doc_storage_account_connection_string: {inbound_doc_storage_account_connection_string}")
    else:
        logging.info(f"inbound_doc_storage_account_connection_string: {inbound_doc_storage_account_connection_string[:4]}...")
    logging.info(f"outbound_doc_storage_account_name: {outbound_doc_storage_account_name}")
    if sensitive:
        logging.info(f"oubdound_doc_storage_creds: {oubdound_doc_storage_creds}")
    else:
        logging.info(f"oubdound_doc_storage_creds: {oubdound_doc_storage_creds[:4]}...")
    logging.info(f"outbound_doc_storage_account_container: {outbound_doc_storage_account_container}")
    logging.info(f"formrecognizerservice: {formrecognizerservice}")
    if sensitive:
        logging.info(f"formrecognizer_creds: {formrecognizer_creds}")
    else:
        logging.info(f"formrecognizer_creds: {formrecognizer_creds[:4]}...")
    logging.info(f"openai_gpt_model: {openai_gpt_model}")
    logging.info(f"openai_emb_model: {openai_emb_model}")
    logging.info(f"openai_api_type: {openai.api_type}")
    logging.info(f"openai.api_base: {openai.api_base}")
    if sensitive:
        logging.info(f"openai_api_key: {openai.api_key}")
    else:
        logging.info(f"openai_api_key: {openai.api_key[:4]}...")


def copy_blob(blob_name: str):
    inbound_connection_string = inbound_doc_storage_account_connection_string
    outbound_connection_string = outbound_doc_storage_account_connection_string
    inbound_container_name = inbound_doc_storage_account_container
    outbound_container_name = outbound_doc_storage_account_container

    # Create BlobServiceClient instances for both inbound and outbound containers
    inbound_blob_service_client = BlobServiceClient.from_connection_string(inbound_connection_string)
    outbound_blob_service_client = BlobServiceClient.from_connection_string(outbound_connection_string)

    # Get a reference to the inbound container and blob
    inbound_container_client = inbound_blob_service_client.get_container_client(inbound_container_name)
    inbound_blob_client = inbound_container_client.get_blob_client(blob_name)

    # Get a reference to the outbound container and blob
    outbound_container_client = outbound_blob_service_client.get_container_client(outbound_container_name)
    outbound_blob_client = outbound_container_client.get_blob_client(blob_name)

    # Copy the blob from the inbound container to the outbound container
    outbound_blob_client.start_copy_from_url(inbound_blob_client.url)

     # Wait for the copy operation to complete
    while True:
        props = outbound_blob_client.get_blob_properties()
        if props.copy.status != 'pending':
            break
        time.sleep(1)

    # Return the URL of the copied blob in the outbound container
    return outbound_blob_client.url


def setup_file_strategy() -> FileStrategy:
    blob_manager = BlobManager(
        endpoint=f"https://{outbound_doc_storage_account_name}.blob.core.windows.net",
        container=outbound_doc_storage_account_container,
        credential=AzureNamedKeyCredential(outbound_doc_storage_account_name, oubdound_doc_storage_creds),
        verbose=True,
    )

    pdf_parser: PdfParser
    pdf_parser = DocumentAnalysisPdfParser(
        endpoint=f"https://{formrecognizerservice}.cognitiveservices.azure.com/",
        credential=AzureKeyCredential(formrecognizer_creds),
        verbose=True,
    )

    embeddings: Optional[OpenAIEmbeddings] = None
    embeddings = AzureOpenAIEmbeddingService(
        open_ai_service=AZURE_OPENAI_SERVICE,
        open_ai_deployment=openai_emb_model,
        open_ai_model_name=openai_emb_model,
        credential=AzureKeyCredential(openai.api_key),
        disable_batch=False,
        verbose=True,
    ) 

    print("Processing file...")
    list_file_strategy: ListFileStrategy
    list_file_strategy = ADLSGen2SingleFileStrategy(
        data_lake_storage_account=outbound_doc_storage_account_name,
        data_lake_filesystem=inbound_doc_storage_account_container,
        data_lake_path="",
        credential=AzureNamedKeyCredential(outbound_doc_storage_account_name, oubdound_doc_storage_creds),
        verbose=True,
    )

    # to be worked out
    document_action = DocumentAction.Add
    #if args.removeall:
    #    document_action = DocumentAction.RemoveAll
    #elif args.remove:
    #    document_action = DocumentAction.Remove
    #else:
    #    document_action = DocumentAction.Add

    return FileStrategy(
        list_file_strategy=list_file_strategy,
        blob_manager=blob_manager,
        pdf_parser=pdf_parser,
        text_splitter=TextSplitter(),
        document_action=document_action,
        embeddings=embeddings,
        search_analyzer_name=search_analyzer_name,
        use_acls=True,
        category=search_service_category,
    )

@app.function_name(name="BlobDocsTrigger")
@app.blob_trigger(arg_name="myblob", path="inbound-docs/{name}", connection="AZURE_INBOUND_DOC_STORAGE_ACCOUNT_CONNECTION_STRING")
async def blob_doc_trigger(myblob: func.InputStream):
    logging.info(f"Python blob trigger function processed blob \n"
                f"Name: {myblob.name}\n"
                f"Blob Size: {myblob.length} bytes")

    log_configuration()

    async def process_files():
        search_info = SearchInfo(
            endpoint=f"https://{search_service_name}.search.windows.net/",
            credential=AzureKeyCredential(search_creds),
            index_name=search_index_name,
            verbose=True,
        )

        logging.info("Copying blob...")
        blob_url = copy_blob(os.path.basename(myblob.name))  # Extract the file name

        logging.info("Before defining file_strategy")
        file_strategy = setup_file_strategy()

        file_strategy.list_file_strategy.file_name=myblob.name

        logging.info("Before running file_strategy")
        await file_strategy.setup(search_info)
        await file_strategy.run(search_info)

        logging.info("After running file_strategy")

    await process_files()



