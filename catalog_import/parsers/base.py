from abc import ABC, abstractmethod


class BaseCatalogParser(ABC):

    @abstractmethod
    def parse(self, rows):
        raise NotImplementedError