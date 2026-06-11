from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class DBObject(Base):
    """Represents a stored CAS blob (model weights, dataset shard, code file)."""
    __tablename__ = "objects"

    hash = Column(String, primary_key=True)
    size = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class DBTree(Base):
    """
    One row per entry inside a Merkle tree node.
    tree_hash + path_name form a composite primary key.
    Either child_tree_hash (sub-directory) or object_hash (leaf blob) is set.
    """
    __tablename__ = "trees"

    tree_hash = Column(String, primary_key=True)
    path_name = Column(String, primary_key=True)
    child_tree_hash = Column(String, nullable=True)
    object_hash = Column(String, ForeignKey("objects.hash"), nullable=True)
    size = Column(Integer, nullable=True)
    type = Column(String)  # 'tree' | 'file' | 'artifact'
    layers = Column(JSON, default=list)


class DBCommit(Base):
    """
    A commit record persisted to PostgreSQL.
    tags   → JSONB array of free-form string labels.
    metrics → JSONB dict of float/int experiment metrics.
    """
    __tablename__ = "commits"

    hash = Column(String, primary_key=True)
    message = Column(String, nullable=False)
    author = Column(String, default="anonymous")
    timestamp = Column(DateTime, default=datetime.utcnow)
    parent_hash = Column(String, ForeignKey("commits.hash"), nullable=True)
    root_tree_hash = Column(String, nullable=False)
    tags = Column(ARRAY(String), default=list)
    metrics = Column(JSON, default=dict)


class DBRef(Base):
    """Branch / tag reference pointing to a commit hash."""
    __tablename__ = "refs"

    name = Column(String, primary_key=True)
    commit_hash = Column(String, ForeignKey("commits.hash"), nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
