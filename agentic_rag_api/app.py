import os
from typing import Annotated, Any, TypedDict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from langgraph.graph import StateGraph, END
from langchain_core.messages import HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_ollama import ChatOllama, OllamaEmbeddings
from langchain_qdrant import QdrantVectorStore
from qdrant_client import QdrantClient


QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "buro_policy")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
OLLAMA_CHAT_MODEL = os.getenv("OLLAMA_CHAT_MODEL", "aya:8b")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
TOP_K = int(os.getenv("TOP_K", "4"))

app = FastAPI(title="Agentic RAG API", version="1.0.0")


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    user: str | None = None
    extra: dict[str, Any] | None = None


class GraphState(TypedDict):
    question: str
    rewritten_question: str | None
    docs: list
    context: str
    answer: str
    route: str


def get_llm() -> ChatOllama:
    return ChatOllama(
        model=OLLAMA_CHAT_MODEL,
        base_url=OLLAMA_BASE_URL,
        temperature=0,
    )


def get_retriever():
    client = QdrantClient(url=QDRANT_URL)
    embeddings = OllamaEmbeddings(
        model=OLLAMA_EMBED_MODEL,
        base_url=OLLAMA_BASE_URL,
    )
    store = QdrantVectorStore(
        client=client,
        collection_name=QDRANT_COLLECTION,
        embedding=embeddings,
    )
    return store.as_retriever(search_kwargs={"k": TOP_K})


router_prompt = ChatPromptTemplate.from_template(
    """Decide if this question needs retrieval from indexed documents.

Question:
{question}

Return only one word:
retrieve
or
direct"""
)

rewrite_prompt = ChatPromptTemplate.from_template(
    """Rewrite the user question for better vector search.
Keep the meaning same.
Return only the rewritten query.

Question:
{question}"""
)

grade_prompt = ChatPromptTemplate.from_template(
    """You are grading whether retrieved context is relevant to the question.

Question:
{question}

Context:
{context}

Return only one word:
relevant
or
irrelevant"""
)

answer_prompt = ChatPromptTemplate.from_template(
    """You are a careful enterprise RAG assistant.

Answer only from the provided context.
If the answer is not in the context, say clearly that it is not available in the indexed documents.

Question:
{question}

Context:
{context}
"""
)


def route_question(state: GraphState) -> GraphState:
    llm = get_llm()
    chain = router_prompt | llm
    result = chain.invoke({"question": state["question"]})
    route = getattr(result, "content", str(result)).strip().lower()
    state["route"] = "retrieve" if "retrieve" in route else "direct"
    return state


def retrieve_docs(state: GraphState) -> GraphState:
    retriever = get_retriever()
    query = state["rewritten_question"] or state["question"]
    docs = retriever.invoke(query)
    state["docs"] = docs
    state["context"] = "\n\n".join(d.page_content for d in docs)
    return state


def rewrite_question(state: GraphState) -> GraphState:
    llm = get_llm()
    chain = rewrite_prompt | llm
    result = chain.invoke({"question": state["question"]})
    state["rewritten_question"] = getattr(result, "content", str(result)).strip()
    return state


def grade_docs(state: GraphState) -> GraphState:
    llm = get_llm()
    chain = grade_prompt | llm
    result = chain.invoke(
        {
            "question": state["question"],
            "context": state.get("context", ""),
        }
    )
    verdict = getattr(result, "content", str(result)).strip().lower()
    state["route"] = "relevant" if "relevant" in verdict else "irrelevant"
    return state


def generate_answer(state: GraphState) -> GraphState:
    llm = get_llm()
    chain = answer_prompt | llm
    result = chain.invoke(
        {
            "question": state["question"],
            "context": state.get("context", ""),
        }
    )
    state["answer"] = getattr(result, "content", str(result))
    return state


def direct_answer(state: GraphState) -> GraphState:
    llm = get_llm()
    result = llm.invoke([HumanMessage(content=state["question"])])
    state["answer"] = getattr(result, "content", str(result))
    return state


def build_graph():
    graph = StateGraph(GraphState)

    graph.add_node("route_question", route_question)
    graph.add_node("retrieve_docs", retrieve_docs)
    graph.add_node("rewrite_question", rewrite_question)
    graph.add_node("grade_docs", grade_docs)
    graph.add_node("generate_answer", generate_answer)
    graph.add_node("direct_answer", direct_answer)

    graph.set_entry_point("route_question")

    graph.add_conditional_edges(
        "route_question",
        lambda s: s["route"],
        {
            "retrieve": "retrieve_docs",
            "direct": "direct_answer",
        },
    )

    graph.add_edge("retrieve_docs", "grade_docs")

    graph.add_conditional_edges(
        "grade_docs",
        lambda s: s["route"],
        {
            "relevant": "generate_answer",
            "irrelevant": "rewrite_question",
        },
    )

    graph.add_edge("rewrite_question", "retrieve_docs")
    graph.add_edge("generate_answer", END)
    graph.add_edge("direct_answer", END)

    return graph.compile()


agent_graph = build_graph()


@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "agentic-rag-api",
        "collection": QDRANT_COLLECTION,
        "chat_model": OLLAMA_CHAT_MODEL,
        "embed_model": OLLAMA_EMBED_MODEL,
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.post("/chat")
def chat(req: ChatRequest):
    try:
        result = agent_graph.invoke(
            {
                "question": req.message,
                "rewritten_question": None,
                "docs": [],
                "context": "",
                "answer": "",
                "route": "",
            }
        )

        sources = []
        for d in result.get("docs", []):
            meta = d.metadata or {}
            sources.append(
                {
                    "source": meta.get("source") or meta.get("file_name") or "unknown",
                    "page": meta.get("page"),
                    "topic": meta.get("topic"),
                }
            )

        return {
            "answer": result.get("answer", ""),
            "sources": sources,
            "session_id": req.session_id,
            "route": result.get("route", ""),
            "rewritten_question": result.get("rewritten_question"),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
