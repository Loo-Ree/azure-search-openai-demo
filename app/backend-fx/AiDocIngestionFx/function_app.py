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
from pypdf import PdfReader, PdfWriter
from azure.identity import AzureDeveloperCliCredential
from azure.core.credentials import AzureKeyCredential
from azure.storage.blob import BlobServiceClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import *
from azure.search.documents import SearchClient
from azure.ai.formrecognizer import DocumentAnalysisClient
import openai

MAX_SECTION_LENGTH = os.environ["MAX_SECTION_LENGTH"] #1000
SENTENCE_SEARCH_LIMIT = os.environ["SENTENCE_SEARCH_LIMIT"] #100
SECTION_OVERLAP = os.environ["SECTION_OVERLAP"] #100
AZURE_OPENAI_SERVICE = os.getenv("AZURE_OPENAI_SERVICE")

localpdfparser = False
search_service_name = os.environ["AZURE_SEARCH_SERVICE"] #os.environ['az_search_service_name']
search_creds = os.environ["AZURE_SEARCH_KEY"] #os.environ['az_search_key']
search_index_name = os.environ["AZURE_SEARCH_INDEX"] #os.environ['az_search_index_name']
search_service_category = os.environ['AZURE_SEARCH_SERVICE_CATEGORY'] #os.environ['az_search_service_category']
inbound_doc_storage_account_connection_string = os.environ['inbound_doc_storage_account_connection_string']
outbound_doc_storage_account_name = os.environ['outbound_doc_storage_account_name']
oubdound_doc_storage_creds = os.environ['outbound_doc_storage_account_key']
outbound_doc_storage_account_container = os.environ['outbound_doc_storage_account_container']
formrecognizerservice = os.environ['form_recognizer_service']
formrecognizer_creds = os.environ['form_recognizer_key']
# Used by the OpenAI SDK
openai_gpt_model = os.environ["AZURE_OPENAI_CHATGPT_MODEL"] #os.environ['openai_gpt_model']
openai_emb_model = os.getenv("AZURE_OPENAI_EMB_MODEL_NAME", "text-embedding-ada-002") #os.environ['openai_emb_model']
openai.api_type =  "azure_ad" #os.environ['openai_api_type'] #azure
openai.api_base = f"https://{AZURE_OPENAI_SERVICE}.openai.azure.com" #f"https://{os.environ['openai_service_name']}.openai.azure.com"
openai.api_version = "2023-07-01-preview" #"2022-12-01"
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


# Learn more at aka.ms/pythonprogrammingmodel

# Get started by running the following code to create a function using a HTTP trigger.

@app.function_name(name="HelloFx")
@app.route(route="hellofx")
def test_function(req: func.HttpRequest) -> func.HttpResponse:
     logging.info('Python HTTP trigger function processed a request.')

     log_configuration()

     name = req.params.get('name')
     if not name:
        try:
            req_body = req.get_json()
        except ValueError:
            pass
        else:
            name = req_body.get('name')

     if name:
        return func.HttpResponse(f"Hello, {name}. This HTTP triggered function executed successfully.")
     else:
        return func.HttpResponse(
             "This HTTP triggered function executed successfully. Pass a name in the query string or in the request body for a personalized response.",
             status_code=200
        )

@app.function_name(name="BlobDocsTrigger")
@app.blob_trigger(arg_name="myblob", path="inbound-docs/{name}", connection="inbound_doc_storage_account_connection_string")
def blob_doc_trigger(myblob: func.InputStream):
    logging.info(f"Python blob trigger function processed blob \n"
                f"Name: {myblob.name}\n"
                f"Blob Size: {myblob.length} bytes")

    log_configuration()

    seekableIo = io.BytesIO(myblob.read())
    upload_blobs(myblob.name, seekableIo)
    page_map = get_document_text(myblob.name, seekableIo)
    sections = create_sections(os.path.basename(myblob.name), page_map, openai_gpt_model, openai_emb_model)
    create_search_index()
    remove_from_index(os.path.basename(myblob.name)) # remove any existing documents for that specific file
    index_sections(os.path.basename(myblob.name), sections)


def blob_name_from_file_page(filename, page = 0):
    if os.path.splitext(filename)[1].lower() == ".pdf":
        return os.path.splitext(os.path.basename(filename))[0] + f"-{page}" + ".pdf"
    else:
        return os.path.basename(filename)


def upload_blobs(filename, blob_stream):
    blob_service = BlobServiceClient(account_url=f"https://{outbound_doc_storage_account_name}.blob.core.windows.net", credential=oubdound_doc_storage_creds)
    blob_container = blob_service.get_container_client(outbound_doc_storage_account_container)
    if not blob_container.exists():
        blob_container.create_container()

    # if file is PDF split into pages and upload each page as a separate blob
    if os.path.splitext(filename)[1].lower() == ".pdf":
        reader = PdfReader(blob_stream)
        pages = reader.pages
        for i in range(len(pages)):
            blob_name = blob_name_from_file_page(filename, i)
            logging.debug(f"\tUploading blob for page {i} -> {blob_name}")
            f = io.BytesIO()
            writer = PdfWriter()
            writer.add_page(pages[i])
            writer.write(f)
            f.seek(0)
            blob_container.upload_blob(blob_name, f, overwrite=True)
    else:
        blob_name = blob_name_from_file_page(filename)
        with open(filename,"rb") as data:
            blob_container.upload_blob(blob_name, data, overwrite=True)

def remove_blobs(filename):
    logging.debug(f"Removing blobs for '{filename or '<all>'}'")
    blob_service = BlobServiceClient(account_url=f"https://{outbound_doc_storage_account_name}.blob.core.windows.net", credential=oubdound_doc_storage_creds)
    blob_container = blob_service.get_container_client(outbound_doc_storage_account_container)
    if blob_container.exists():
        if filename == None:
            blobs = blob_container.list_blob_names()
        else:
            prefix = os.path.splitext(os.path.basename(filename))[0]
            blobs = filter(lambda b: re.match(f"{prefix}-\d+\.pdf", b), blob_container.list_blob_names(name_starts_with=os.path.splitext(os.path.basename(prefix))[0]))
        for b in blobs:
            logging.debug(f"\tRemoving blob {b}")
            blob_container.delete_blob(b)

def table_to_html(table):
    table_html = "<table>"
    rows = [sorted([cell for cell in table.cells if cell.row_index == i], key=lambda cell: cell.column_index) for i in range(table.row_count)]
    for row_cells in rows:
        table_html += "<tr>"
        for cell in row_cells:
            tag = "th" if (cell.kind == "columnHeader" or cell.kind == "rowHeader") else "td"
            cell_spans = ""
            if cell.column_span > 1: cell_spans += f" colSpan={cell.column_span}"
            if cell.row_span > 1: cell_spans += f" rowSpan={cell.row_span}"
            table_html += f"<{tag}{cell_spans}>{html.escape(cell.content)}</{tag}>"
        table_html +="</tr>"
    table_html += "</table>"
    return table_html

def get_document_text(filename, blob_stream: io.BytesIO):
    blob_stream.seek(0)
    offset = 0
    page_map = []
    if localpdfparser:
        reader = PdfReader(blob_stream)
        pages = reader.pages
        for page_num, p in enumerate(pages):
            page_text = p.extract_text()
            page_map.append((page_num, offset, page_text))
            offset += len(page_text)
    else:
        logging.debug(f"Extracting text from '{filename}' using Azure Form Recognizer")
        form_recognizer_client = DocumentAnalysisClient(endpoint=f"https://{formrecognizerservice}.cognitiveservices.azure.com/", credential=AzureKeyCredential(formrecognizer_creds), headers={"x-ms-useragent": "azure-search-chat-demo/1.0.0"})
        #with open(filename, "rb") as f:
        poller = form_recognizer_client.begin_analyze_document("prebuilt-layout", document = blob_stream)
        form_recognizer_results = poller.result()

        for page_num, page in enumerate(form_recognizer_results.pages):
            tables_on_page = [table for table in form_recognizer_results.tables if table.bounding_regions[0].page_number == page_num + 1]

            # mark all positions of the table spans in the page
            page_offset = page.spans[0].offset
            page_length = page.spans[0].length
            table_chars = [-1]*page_length
            for table_id, table in enumerate(tables_on_page):
                for span in table.spans:
                    # replace all table spans with "table_id" in table_chars array
                    for i in range(span.length):
                        idx = span.offset - page_offset + i
                        if idx >=0 and idx < page_length:
                            table_chars[idx] = table_id

            # build page text by replacing charcters in table spans with table html
            page_text = ""
            added_tables = set()
            for idx, table_id in enumerate(table_chars):
                if table_id == -1:
                    page_text += form_recognizer_results.content[page_offset + idx]
                elif not table_id in added_tables:
                    page_text += table_to_html(tables_on_page[table_id])
                    added_tables.add(table_id)

            page_text += " "
            page_map.append((page_num, offset, page_text))
            offset += len(page_text)

    return page_map

def split_text(page_map, filename):
    SENTENCE_ENDINGS = [".", "!", "?"]
    WORDS_BREAKS = [",", ";", ":", " ", "(", ")", "[", "]", "{", "}", "\t", "\n"]
    logging.debug(f"Splitting '{filename}' into sections")

    def find_page(offset):
        l = len(page_map)
        for i in range(l - 1):
            if offset >= page_map[i][1] and offset < page_map[i + 1][1]:
                return i
        return l - 1

    all_text = "".join(p[2] for p in page_map)
    length = len(all_text)
    start = 0
    end = length
    while start + SECTION_OVERLAP < length:
        last_word = -1
        end = start + MAX_SECTION_LENGTH

        if end > length:
            end = length
        else:
            # Try to find the end of the sentence
            while end < length and (end - start - MAX_SECTION_LENGTH) < SENTENCE_SEARCH_LIMIT and all_text[end] not in SENTENCE_ENDINGS:
                if all_text[end] in WORDS_BREAKS:
                    last_word = end
                end += 1
            if end < length and all_text[end] not in SENTENCE_ENDINGS and last_word > 0:
                end = last_word # Fall back to at least keeping a whole word
        if end < length:
            end += 1

        # Try to find the start of the sentence or at least a whole word boundary
        last_word = -1
        while start > 0 and start > end - MAX_SECTION_LENGTH - 2 * SENTENCE_SEARCH_LIMIT and all_text[start] not in SENTENCE_ENDINGS:
            if all_text[start] in WORDS_BREAKS:
                last_word = start
            start -= 1
        if all_text[start] not in SENTENCE_ENDINGS and last_word > 0:
            start = last_word
        if start > 0:
            start += 1

        section_text = all_text[start:end]
        yield (section_text, find_page(start))

        last_table_start = section_text.rfind("<table")
        if (last_table_start > 2 * SENTENCE_SEARCH_LIMIT and last_table_start > section_text.rfind("</table")):
            # If the section ends with an unclosed table, we need to start the next section with the table.
            # If table starts inside SENTENCE_SEARCH_LIMIT, we ignore it, as that will cause an infinite loop for tables longer than MAX_SECTION_LENGTH
            # If last table starts inside SECTION_OVERLAP, keep overlapping
            logging.debug(f"Section ends with unclosed table, starting next section with the table at page {find_page(start)} offset {start} table start {last_table_start}")
            start = min(end - SECTION_OVERLAP, start + last_table_start)
        else:
            start = end - SECTION_OVERLAP
        
    if start + SECTION_OVERLAP < end:
        yield (all_text[start:end], find_page(start))

@retry(wait=wait_random_exponential(min=1, max=20), stop=stop_after_attempt(10))
def create_sections(filename, page_map, gpt3model, emb_model):
    for i, (section, pagenum) in enumerate(split_text(page_map, filename)):
        logging.debug(f"{filename}-{i}")

        entities = get_entities(section, gpt3model, 0)
        title = get_title(section,gpt3model,0)

        yield {
            "id": re.sub("[^0-9a-zA-Z_-]","_",f"{filename}-{i}"),
            "content": section,
            "embedding": openai.Embedding.create(engine=emb_model, input=section)["data"][0]["embedding"],
            "category": search_service_category,
            "sourcepage": blob_name_from_file_page(filename, pagenum),
            "sourcefile": filename,
            "entities": entities,
            "title":title
        }

def get_title(section, gpt3model, trnumber):
    prompt = "Genera una sintesi del seguente testo."\
        "\ntesto:"\
        f"\n{section}"\
        "\nfine testo."\
        "\nSintesi:\n"       
      
    try:
        completion = openai.Completion.create(
            engine=gpt3model,
            prompt=prompt,
            temperature=0,
            max_tokens=300)
    except  Exception as e:
        err = str(e)
        logging.error(f"Errore: {err}")
        time.sleep(5) 
        trnumber = trnumber+1
        # openai.api_key = azd_credential.get_token("https://cognitiveservices.azure.com/.default").token
        if trnumber < 2:
            return get_title(section,gpt3model,trnumber)
        else:
            return ""
        
    sintesi =  completion.choices[0].text
            
    return sintesi;

def get_entities(section,gpt3model, trnumber):
    prompt = "estrai le entità dal testo ed i suoi sinonimi in un unica lista (esempio formato: entità1,entità2,...,sinonimo1,sinonimo2,...)."\
"Escludi dai risultati tag XML e HTML. Escludi date e informazioni personali. Non inserire spazi dopo la virgola. Non andare mai a capo. Non includere simboli di punteggiatura."\
"\ntesto:"\
f"{section}"\
"\nfine testo."\
"\nEntità:"
    
    try:
        completion = openai.Completion.create(
            engine=gpt3model,
            prompt=prompt,
            temperature=0,
            max_tokens=500)
    except  Exception as e:
        err = str(e)
        logging.warn(f"Errore: {err}")
        time.sleep(5) 
        trnumber = trnumber+1
        #openai.api_key = azd_credential.get_token("https://cognitiveservices.azure.com/.default").token
        if trnumber < 2:
            return get_entities(section,gpt3model,trnumber)
        else:
            return []
        
    entitiesStr =  completion.choices[0].text
    entities = entitiesStr.split(",")        
    #return json.dumps(entities);
    return entities

def create_search_index():
    logging.debug(f"Ensuring search index {search_index_name} exists")
    index_client = SearchIndexClient(endpoint=f"https://{search_service_name}.search.windows.net/",
                                     credential=AzureKeyCredential(search_creds))
    if search_index_name not in index_client.list_index_names():
        index = SearchIndex(
            name=search_index_name,
            fields=[
                SimpleField(name="id", type="Edm.String", key=True),
                SearchableField(name="content", type="Edm.String", analyzer_name="it.microsoft"),
                SearchField(name="embedding", type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                hidden=False, searchable=True, filterable=False, sortable=False, facetable=False,
                dimensions=1536, vector_search_configuration="default"),
                SimpleField(name="category", type="Edm.String", filterable=True, facetable=True),
                SimpleField(name="sourcepage", type="Edm.String", filterable=True, facetable=True),
                SearchableField(name="sourcefile", type="Edm.String", analyzer_name="it.microsoft",filterable=True),
                SearchableField(name="entities", collection=True, type="Edm.String", analyzer_name="it.microsoft",filterable=True),
                SearchableField(name="title", type="Edm.String", analyzer_name="it.microsoft")
            ],
            semantic_settings=SemanticSettings(
                configurations=[SemanticConfiguration(
                    name='default',
                    prioritized_fields=PrioritizedFields(
                        title_field=SemanticField(field_name='title'), prioritized_content_fields=[SemanticField(field_name='content')]))]),
            vector_search=VectorSearch(
                algorithm_configurations=[
                    VectorSearchAlgorithmConfiguration(
                        name="default",
                        kind="hnsw",
                        hnsw_parameters=HnswParameters(metric="cosine")
                    )
                ]
            )
        )
        logging.debug(f"Creating {search_index_name} search index")
        index_client.create_index(index)
    else:
        logging.debug(f"Search index {search_index_name} already exists")

def index_sections(filename, sections):
    logging.debug(f"Indexing sections from '{filename}' into search index '{search_index_name}'")
    search_client = SearchClient(endpoint=f"https://{search_service_name}.search.windows.net/",
                                    index_name=search_index_name,
                                    credential=AzureKeyCredential(search_creds))
    i = 0
    batch = []
    for s in sections:
        batch.append(s)
        i += 1
        if i % 1000 == 0:
            results = search_client.upload_documents(documents=batch)
            succeeded = sum([1 for r in results if r.succeeded])
            logging.debug(f"\tIndexed {len(results)} sections, {succeeded} succeeded")
            batch = []

    if len(batch) > 0:
        results = search_client.upload_documents(documents=batch)
        succeeded = sum([1 for r in results if r.succeeded])
        logging.debug(f"\tIndexed {len(results)} sections, {succeeded} succeeded")

def remove_from_index(filename):
    logging.debug(f"Removing sections from '{filename or '<all>'}' from search index '{search_index_name}'")
    search_client = SearchClient(endpoint=f"https://{search_service_name}.search.windows.net/",
                                    index_name=search_index_name,
                                    credential=AzureKeyCredential(search_creds))
    while True:
        filter = None if filename == None else f"sourcefile eq '{os.path.basename(filename)}'"
        r = search_client.search("", filter=filter, top=1000, include_total_count=True)
        if r.get_count() == 0:
            break
        r = search_client.delete_documents(documents=[{ "id": d["id"] } for d in r])
        logging.debug(f"\tRemoved {len(r)} sections from index")
        # It can take a few seconds for search results to reflect changes, so wait a bit
        time.sleep(2)