from sqlalchemy import Column, String, Integer, JSON, DateTime, ForeignKey, Table, Boolean
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime

Base = declarative_base()

class DBObject(Base):
    __tablename__ = 'objects'
    hash = Column(String, primary_key=True)
    size = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)

class DBTree(Base):
    __tablename__ = 'trees'
    # Composite PK: tree_hash and path_name
    tree_hash = Column(String, primary_key=True)
    path_name = Column(String, primary_key=True)
    
    # Either points to another tree (folder) or an object (file/layer)
    child_tree_hash = Column(String, nullable=True)
    object_hash = Column(String, ForeignKey('objects.hash'), nullable=True)
    
    # Metadata for the entry
    size = Column(Integer, nullable=True)
    type = Column(String) # 'tree', 'file', 'layer'

class DBCommit(Base):
    __tablename__ = 'commits'
    hash = Column(String, primary_key=True)
    message = Column(String)
    author = Column(String)
    timestamp = Column(DateTime, default=datetime.utcnow)
    metrics = Column(JSON)
    parent_hash = Column(String, ForeignKey('commits.hash'), nullable=True)
    
    # Root of the Merkle Tree
    root_tree_hash = Column(String) # References DBTree.tree_hash

class DBRef(Base):
    __tablename__ = 'refs'
    name = Column(String, primary_key=True)
    commit_hash = Column(String, ForeignKey('commits.hash'))
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
