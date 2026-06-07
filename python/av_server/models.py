from sqlalchemy import Column, String, Integer, JSON, DateTime, ForeignKey, Table
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime

Base = declarative_base()

# Many-to-Many relationship between Commits and Objects
commit_objects = Table(
    'commit_objects',
    Base.metadata,
    Column('commit_hash', String, ForeignKey('commits.hash'), primary_key=True),
    Column('object_hash', String, ForeignKey('objects.hash'), primary_key=True),
    Column('path', String, primary_key=True),
    Column('size', Integer),
    Column('type', String)
)

class DBObject(Base):
    __tablename__ = 'objects'
    hash = Column(String, primary_key=True)
    size = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)

class DBCommit(Base):
    __tablename__ = 'commits'
    hash = Column(String, primary_key=True)
    message = Column(String)
    author = Column(String)
    timestamp = Column(DateTime, default=datetime.utcnow)
    metrics = Column(JSON)
    parent_hash = Column(String, ForeignKey('commits.hash'), nullable=True)
    
    objects = relationship("DBObject", secondary=commit_objects)

class DBRef(Base):
    __tablename__ = 'refs'
    name = Column(String, primary_key=True)
    commit_hash = Column(String, ForeignKey('commits.hash'))
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
