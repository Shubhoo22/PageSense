
import os
from collections import deque
from functools import lru_cache
from typing import Dict

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from langchain_core.tools import tool
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_community.tools import DuckDuckGoSearchRun
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_groq import ChatGroq


load_dotenv()

app = FastAPI(
    title="PageSense API",
    description="AI-powered webpage question answering using RAG",
    version="1.0.0"
)

# CORS configuration for Chrome extension communication
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Session conversation history
session_db: Dict[str, deque] = {}


# Load embedding model only when required
@lru_cache(maxsize=1)
def get_embeddings():
    return HuggingFaceEmbeddings(
        model_name="BAAI/bge-small-en"
    )


# Initialize Groq model only when required
@lru_cache(maxsize=1)
def get_groq_model():
    api_key = os.getenv("GROQ_API_KEY")

    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="GROQ_API_KEY is not configured."
        )

    return ChatGroq(
        api_key=api_key,
        model="openai/gpt-oss-120b",
        temperature=0
    )


# Web search tool
@tool
def web_search(query: str):
    """Search the web when webpage context does not contain the answer."""
    try:
        search = DuckDuckGoSearchRun()
        return search.run(query)
    except Exception as error:
        return f"Web search failed: {str(error)}"


# Request model
class RAGrequest(BaseModel):
    text: str = Field(default="", max_length=500000)
    query: str = Field(min_length=1, max_length=10000)
    session_id: str = Field(min_length=1, max_length=200)


# Health endpoint
@app.get("/health")
def health():
    return {"status": "ok"}


# Root endpoint
@app.get("/")
def home():
    return {
        "message": "Welcome to PageSense API",
        "status": "running",
        "docs": "/docs"
    }


# Main RAG endpoint
@app.post("/chat")
def get_answer(payload: RAGrequest):

    try:
        # Initialize conversation history
        if payload.session_id not in session_db:
            session_db[payload.session_id] = deque(maxlen=5)

        history = session_db[payload.session_id]

        history_str = ""

        for user_msg, ai_msg in history:
            history_str += (
                f"User: {user_msg}\n"
                f"AI: {ai_msg}\n"
            )

        # Prepare webpage context
        context = ""

        if payload.text.strip():

            splitter = RecursiveCharacterTextSplitter(
                chunk_size=500,
                chunk_overlap=50
            )

            chunks = splitter.split_text(payload.text)

            if chunks:
                docs = [
                    Document(page_content=chunk)
                    for chunk in chunks
                ]

                vectorstore = FAISS.from_documents(
                    documents=docs,
                    embedding=get_embeddings()
                )

                retriever = vectorstore.as_retriever(
                    search_type="similarity",
                    search_kwargs={"k": 3}
                )

                relevant_docs = retriever.invoke(payload.query)

                context = "\n\n".join(
                    doc.page_content
                    for doc in relevant_docs
                )

        # Initialize Groq model
        groq_model = get_groq_model()

        groq_model_with_tools = groq_model.bind_tools(
            [web_search]
        )

        # Construct prompt
        messages = [
            SystemMessage(
                content="""You are PageSense, a helpful AI webpage assistant.

RULES:
1. Answer using the provided webpage context and conversation history.
2. If the webpage context is insufficient or unrelated,
   use the web_search tool.
3. Do not invent facts.
4. Do not start answers with phrases like
   'Based on the context' or 'According to the document'.
5. Give clear and direct answers."""
            ),

            HumanMessage(
                content=f"""Conversation History:
{history_str}

Webpage Context:
{context}

User's Question:
{payload.query}

Answer the user's question."""
            )
        ]

        # Generate initial response
        response = groq_model_with_tools.invoke(messages)

        # Handle web search tool call
        if response.tool_calls:

            tool_call = response.tool_calls[0]

            if tool_call["name"] == "web_search":

                search_query = tool_call["args"].get(
                    "query",
                    payload.query
                )

                print(f"Web Search Called: {search_query}")

                search_result = web_search.invoke(
                    {"query": search_query}
                )

                # Generate final answer using search results
                agent_prompt = f"""
You are PageSense, a helpful AI assistant.

Conversation History:
{history_str}

Webpage Context:
{context}

Web Search Results:
{search_result}

User's Question:
{payload.query}

Answer accurately using the available information.
Do not invent facts.
Give a direct and clear answer.
"""

                final_response = groq_model.invoke(agent_prompt)

                answer = final_response.content

            else:
                answer = response.content

        else:
            answer = response.content

        # Ensure response is a string
        if not isinstance(answer, str):
            answer = str(answer)

        # Save conversation history
        history.append(
            (payload.query, answer)
        )

        return {
            "answer": answer
        }

    except HTTPException:
        raise

    except Exception as error:
        print(f"PageSense API Error: {error}")

        raise HTTPException(
            status_code=500,
            detail="An error occurred while processing your question."
        )
