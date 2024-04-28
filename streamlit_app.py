import os
import tempfile
import streamlit as st
from langchain.llms import OpenAI
from langchain.llms import LlamaCpp
from langchain.document_loaders import PyPDFLoader
from langchain.memory import ConversationBufferMemory
from langchain.memory.chat_message_histories import StreamlitChatMessageHistory
from langchain.embeddings import HuggingFaceEmbeddings
from langchain.callbacks.base import BaseCallbackHandler
from langchain.chains import ConversationalRetrievalChain
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.vectorstores import FAISS

from torch import cuda, bfloat16
import transformers

st.set_page_config(page_title="Chat with PDF", page_icon="🦜")
st.title("🦜 Chat with PDF")

@st.cache_resource(ttl="1h")
def configure_retriever(uploaded_files):
    docs = []
    temp_dir = tempfile.TemporaryDirectory()
    for file in uploaded_files:
        temp_filepath = os.path.join(temp_dir.name, file.name)
        with open(temp_filepath, "wb") as f:
            f.write(file.getvalue())
        loader = PyPDFLoader(temp_filepath)
        docs.extend(loader.load())

    text_splitter = RecursiveCharacterTextSplitter(chunk_size=10000, chunk_overlap=20)
    splits = text_splitter.split_documents(docs)

    embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
    vectordb = FAISS.from_documents(splits, embedding=embeddings)

    retriever = vectordb.as_retriever(search_kwargs={"k": 2})

    return retriever

class StreamHandler(BaseCallbackHandler):
    def __init__(self, container: st.delta_generator.DeltaGenerator, initial_text: str = ""):
        self.container = container
        self.text = initial_text
        self.run_id_ignore_token = None

    def on_llm_start(self, serialized: dict, prompts: list, **kwargs):
        # Workaround to prevent showing the rephrased question as output
        if prompts[0].startswith("Human"):
            self.run_id_ignore_token = kwargs.get("run_id")

    def on_llm_new_token(self, token: str, **kwargs) -> None:
        if self.run_id_ignore_token == kwargs.get("run_id", False):
            return
        self.text += token
        self.container.markdown(self.text)

class PrintRetrievalHandler(BaseCallbackHandler):
    def __init__(self, container):
        self.status = container.status("**Context Retrieval**")

    def on_retriever_start(self, serialized: dict, query: str, **kwargs):
        self.status.write(f"**Question:** {query}")
        self.status.update(label=f"**Context Retrieval:** {query}")

    def on_retriever_end(self, documents, **kwargs):
        for idx, doc in enumerate(documents):
            source = os.path.basename(doc.metadata["source"])
            self.status.write(f"**Document {idx} from {source}**")
            self.status.markdown(doc.page_content)
        self.status.update(state="complete")

uploaded_files = st.sidebar.file_uploader(
    label="Upload PDF files", type=["pdf"], accept_multiple_files=True
)
if not uploaded_files:
    st.info("Please upload PDF documents to continue.")
    st.stop()

retriever = configure_retriever(uploaded_files)

msgs = StreamlitChatMessageHistory()
memory = ConversationBufferMemory(memory_key="chat_history", return_messages=True)


import torch
from torch import cuda, bfloat16
import transformers

# model_id = 'PY007/TinyLlama-1.1B-Chat-v0.3'
model_id = 'meta-llama/Llama-2-7b-chat-hf'
device = f'cuda:{cuda.current_device()}' if torch.cuda.is_available() else 'cpu'
print(device)
bnb_config = transformers.BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type='nf4',
    bnb_4bit_use_double_quant=True,
    bnb_4bit_compute_dtype=bfloat16
)

hf_auth = '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'
model_config = transformers.AutoConfig.from_pretrained(
    model_id,
    token=hf_auth,
)

# Load the model specifying the device explicitly
model = transformers.AutoModelForCausalLM.from_pretrained(
    model_id,
    trust_remote_code=True,
    config=model_config,
    quantization_config=bnb_config,
    device_map="auto",
    low_cpu_mem_usage=True,
    # device=device,  # Specify the device here
    # token=hf_auth
).cpu()

model.eval()
print(f"Model loaded on {device}")


# We can use Python LlamaCpp - but this is way slower on my machine than the LM Studio (with llamap.cpp) option


from transformers import AutoTokenizer, AutoModelForSeq2SeqLM,pipeline
from langchain import HuggingFacePipeline

tokenizer = transformers.AutoTokenizer.from_pretrained(
    model_id,
    token=hf_auth
)
pipe = pipeline("text2text-generation", model=model, tokenizer=tokenizer)
llm = HuggingFacePipeline(
    pipeline = pipe,
    model_kwargs={"temperature": 0, "max_length": 1024},
)

# llm = LlamaCpp(
#    model_path="dolphin-2.2.1-mistral-7B-GGUF/dolphin-2.2.1-mistral-7b.Q5_K_M.gguf",
#    temperature=0.0,
#    n_ctx=8192,
#    n_gpu_layers=1,
#    streaming=True
# )

qa_chain = ConversationalRetrievalChain.from_llm(
    llm, chain_type='stuff', retriever=retriever, memory=memory
)

if len(msgs.messages) == 0 or st.sidebar.button("Clear message history"):
    msgs.clear()
    msgs.add_ai_message("How can I help you?")

avatars = {"human": "user", "ai": "assistant"}
for msg in msgs.messages:
    st.chat_message(avatars[msg.type]).write(msg.content)

if user_query := st.chat_input(placeholder="Ask me anything!"):
    st.chat_message("user").write(user_query)

    with st.chat_message("assistant"):
        retrieval_handler = PrintRetrievalHandler(st.container())
        stream_handler = StreamHandler(st.empty())
        response = qa_chain.run(user_query, callbacks=[retrieval_handler, stream_handler])
